"""
Backtest runner script — runs single or batch backtests.

Usage:
    # Single backtest
    python scripts/run_backtest.py --symbol HYPEUSD --timeframe 5m

    # Batch optimization across all symbols
    python scripts/run_backtest.py --batch --timeframes 1m,3m,5m

    # Batch on specific symbols
    python scripts/run_backtest.py --batch --symbols HYPEUSD,SUIUSD
"""

from __future__ import annotations

import argparse
import logging
import sys

from rich.console import Console # type: ignore
from rich.table import Table # type: ignore
import pandas as pd
import pathlib

sys.path.insert(0, str(__file__).rsplit("scripts", 1)[0])

from src.config import load_config # type: ignore
from src.backtest.engine import BacktestEngine, BacktestResult # type: ignore
from src.backtest.optimizer import BatchOptimizer # type: ignore
from src.backtest.report import generate_html_report # type: ignore
from src.data.binance import o1_to_binance # type: ignore
from src.data.storage import load_candles # type: ignore

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

console = Console()


def run_single(symbol: str, timeframe: str) -> BacktestResult | None:
    """Run a single backtest."""
    cfg = load_config()
    cfg.timeframe = timeframe

    binance_sym = o1_to_binance(symbol)
    if not binance_sym:
        console.print(f"[red]No Binance mapping for {symbol}[/]")
        return None

    df = load_candles(binance_sym, timeframe)
    if df.empty:
        console.print(f"[red]No data for {binance_sym} {timeframe}. Run fetch_history.py first.[/]")
        return None

    console.print(f"\n[bold cyan]Backtesting {symbol} ({binance_sym}) | {timeframe}[/]")
    console.print(f"   Data: {len(df)} candles ({df.index[0]} → {df.index[-1]})")
    console.print(f"   Capital: ${cfg.backtest.initial_capital}\n")

    engine = BacktestEngine(cfg)
    result = engine.run(df, symbol=symbol)

    _print_result(result)

    # Generate HTML report
    report_path = generate_html_report(result)
    console.print(f"\n[bold green]Report: {report_path}[/]")

    return result


def run_batch(symbols: list[str], timeframes: list[str], max_workers: int = 4):
    """Run batch optimization."""
    cfg = load_config()
    target_symbols = symbols or cfg.active_symbols

    # Build symbol pairs
    pairs = []
    for o1_sym in target_symbols:
        binance_sym = o1_to_binance(o1_sym)
        if binance_sym:
            pairs.append((o1_sym, binance_sym))

    if not pairs:
        console.print("[red]No valid symbol mappings found[/]")
        return

    console.print(f"\n[bold cyan]Batch Optimization[/]")
    console.print(f"   Symbols: {len(pairs)}")
    console.print(f"   Timeframes: {', '.join(timeframes)}")
    console.print(f"   Workers: {max_workers}\n")

    optimizer = BatchOptimizer(
        timeframes=timeframes,
        max_workers=max_workers,
    )
    opt_result = optimizer.run(pairs)

    console.print(f"\n[bold]Completed in {opt_result.elapsed_seconds:.0f}s[/]")
    console.print(f"Total runs: {opt_result.total_configs}")
    console.print(f"Valid results: {len(opt_result.results)}")

    if opt_result.best_by_sharpe:
        console.print("\n[bold green]Best by Sharpe:[/]")
        _print_result(opt_result.best_by_sharpe)

    if opt_result.best_by_calmar:
        console.print("\n[bold blue]Best by Calmar:[/]")
        _print_result(opt_result.best_by_calmar)

    # Summary table
    summary = opt_result.summary_df()
    if not summary.empty:
        table = Table(title="Top 20 Configurations by Sharpe")
        for col in summary.columns[:10]:
            table.add_column(col, style="cyan" if col == "symbol" else "white")
        for row in summary.head(20).itertuples(index=False):
            table.add_row(*[f"{v:.3f}" if isinstance(v, float) else str(v) for v in row[:10]])
        console.print(table)

        # Save to CSV
        csv_path = "reports/batch_results.csv"
        summary.to_csv(csv_path, index=False)
        console.print(f"\n[bold green]Full results saved: {csv_path}[/]")

    # Identify problematic symbols (negative Sharpe across all configs)
    if not summary.empty:
        sym_perf = summary.groupby("symbol")["sharpe"].max()
        bad_symbols = sym_perf[sym_perf < 0].index.tolist()
        if bad_symbols:
            console.print(f"\n[bold red]Symbols to exclude (negative Sharpe):[/]")
            for s in bad_symbols:
                console.print(f"  [red]✗ {s}[/]")


def _print_result(r: BacktestResult):
    """Print formatted backtest result."""
    color = "green" if r.total_return_pct > 0 else "red"
    console.print(f"  Symbol:     [bold]{r.symbol}[/] | {r.timeframe}")
    console.print(f"  Period:     {r.start_date[:10]} → {r.end_date[:10]}")
    console.print(f"  Capital:    ${r.initial_capital} → ${r.final_capital}")
    console.print(f"  Return:     [{color}]{r.total_return_pct:+.2f}%[/]")
    console.print(f"  Sharpe:     {r.sharpe_ratio:.3f}")
    console.print(f"  Sortino:    {r.sortino_ratio:.3f}")
    console.print(f"  Calmar:     {r.calmar_ratio:.3f}")
    console.print(f"  Max DD:     [red]{r.max_drawdown_pct:.2f}%[/]")
    console.print(f"  Win Rate:   {r.win_rate:.1f}%")
    console.print(f"  PF:         {r.profit_factor:.3f}")
    console.print(f"  Trades:     {r.total_trades} ({r.winning_trades}W / {r.losing_trades}L)")
    console.print(f"  Fees:       ${r.total_fees:.2f}")


def main():
    parser = argparse.ArgumentParser(description="Run backtests")
    parser.add_argument("--symbol", type=str, help="Single symbol to backtest (01 format)")
    parser.add_argument("--timeframe", type=str, default="5m", help="Timeframe")
    parser.add_argument("--batch", action="store_true", help="Run batch optimization")
    parser.add_argument("--symbols", type=str, help="Comma-separated symbols for batch")
    parser.add_argument("--timeframes", type=str, default="1m,3m,5m", help="Timeframes for batch")
    parser.add_argument("--workers", type=int, default=4, help="Parallel workers")
    args = parser.parse_args()

    if args.batch:
        symbols = args.symbols.split(",") if args.symbols else []
        timeframes = args.timeframes.split(",")
        run_batch(symbols, timeframes, args.workers)
    elif args.symbol:
        run_single(args.symbol, args.timeframe)
    else:
        console.print("[yellow]Usage: --symbol HYPEUSD or --batch[/]")


if __name__ == "__main__":
    main()
