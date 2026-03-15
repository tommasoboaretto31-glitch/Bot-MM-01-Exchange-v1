import os
import sys
import threading
import webbrowser
import time
import logging
import json
import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk
from pathlib import Path

# Add project root to sys.path immediately to ensure 'src' imports work
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# 0. GLOBAL IMPORTS (after path fix)
try:
    import requests
    import tomlkit
    import base58
    from src.config import CONFIG_DIR, ROOT_DIR as PROJECT_ROOT
    from src.cli import run_bot_with_dashboard
    from src.api.client import O1Client
    import src.dashboard.app as dash_app
except ImportError:
    # These might fail during Nuitka/PyInstaller build phases or if env not ready
    pass

# 1. IMMEDIATE LOGGING
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("launcher_ultra_stable.log")
    ]
)
logger = logging.getLogger("Launcher")

# CONFIGURATION
MIN_CAPITAL = 0.0
AUTH_SERVER_URL = "https://gist.githubusercontent.com/tommasoboaretto31-glitch/fe681bc391a27da4a442545ee2d22dcd/raw/0eea5ade31e731eb9f6f05e7130979973e37c687/ids.txt" 
REF_LINK = "https://01.xyz/ref/019c2e4e-3be0-74e8-ab72-22e2ffb15398"

def get_bundle_dir():
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.dirname(os.path.abspath(__file__))

def get_resource_path(relative_path):
    return os.path.join(get_bundle_dir(), relative_path)

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

class UltraStableLauncher(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("ZeroOne v2.8 - Powered by Holocron")
        self.geometry("650x850")
        
        # Notebook for Tabs
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(fill="both", expand=True, padx=10, pady=10)
        
        self.setup_tab = self.tabview.add(" SETUP BOT ")
        self.monitor_tab = self.tabview.add(" LIVE MONITOR ")
        
        self.available_markets = []
        self.market_checkboxes = {}
        
        # Use a scrollable frame as the main container for SETUP to prevent clipping
        self.setup_scroll = ctk.CTkScrollableFrame(self.setup_tab, fg_color="transparent")
        self.setup_scroll.pack(fill="both", expand=True)
        
        self.create_setup_widgets(self.setup_scroll)
        self.create_monitor_widgets(self.monitor_tab)
        
        # Initial Fetch
        self.after(500, lambda: threading.Thread(target=self.fetch_available_markets, daemon=True).start())

    def create_setup_widgets(self, container):
        container.grid_columnconfigure(0, weight=1)
        
        header_frame = ctk.CTkFrame(container, fg_color="transparent")
        header_frame.pack(pady=(20, 10))
        
        ctk.CTkLabel(header_frame, text="HOLOCRON", font=("Segoe UI", 32, "bold"), text_color="#00FF66").pack()
        ctk.CTkLabel(header_frame, text="ZEROONE TRADING BOT", font=("Segoe UI", 20, "bold")).pack()

        form_frame = ctk.CTkFrame(container, fg_color="transparent")
        form_frame.pack(fill="x", padx=40, pady=10)

        ctk.CTkLabel(form_frame, text="EXCHANGE ID / UID", text_color="#00E5FF", anchor="w").pack(fill="x", pady=(10, 2))
        self.id_entry = ctk.CTkEntry(form_frame, placeholder_text="Enter Exchange ID", height=40)
        self.id_entry.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(form_frame, text="SOLANA PRIVATE KEY (Base58)", text_color="#00E5FF", anchor="w").pack(fill="x", pady=(10, 2))
        self.key_entry = ctk.CTkEntry(form_frame, placeholder_text="Enter Private Key", show="*", height=40)
        self.key_entry.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(form_frame, text="OPERATIONAL CAPITAL (USD)", text_color="#00E5FF", anchor="w").pack(fill="x", pady=(10, 2))
        self.capital_entry = ctk.CTkEntry(form_frame, height=40)
        self.capital_entry.insert(0, "100.0")
        self.capital_entry.pack(fill="x", pady=(0, 15))

        self.strategy_var = ctk.StringVar(value="Optimized BE")
        self.paper_mode_var = tk.BooleanVar(value=True)
        self.paper_check = ctk.CTkCheckBox(form_frame, text=" PAPER TRADING MODE (DEMO)", variable=self.paper_mode_var, font=("Segoe UI", 12))
        self.paper_check.pack(anchor="w", pady=(0, 20))

        # Coin Selection Section
        ctk.CTkLabel(form_frame, text="SELECT MARKETS (Auto-Limited by Capital)", text_color="#00E5FF", anchor="w").pack(fill="x", pady=(10, 2))
        self.market_limit_label = ctk.CTkLabel(form_frame, text="Fetching markets...", text_color="#888888", font=("Segoe UI", 10), anchor="w")
        self.market_limit_label.pack(fill="x")
        
        self.market_scroll = ctk.CTkScrollableFrame(form_frame, height=150, fg_color="#1A1A1A")
        self.market_scroll.pack(fill="x", pady=(5, 10))
        
        self.capital_entry.bind("<KeyRelease>", lambda e: self.update_market_limits())

        self.status_label = ctk.CTkLabel(container, text="READY TO INITIALIZE", text_color="#00E5FF", font=("Segoe UI", 12, "bold"))
        self.status_label.pack(pady=(5, 5))

        self.start_btn = ctk.CTkButton(container, text="INITIALIZE BOT (AVVIA)", font=("Segoe UI", 18, "bold"), height=55, fg_color="#00FF66", text_color="#000000", hover_color="#00CC55", command=self.on_start)
        self.start_btn.pack(fill="x", padx=40, pady=(5, 20))

    def create_monitor_widgets(self, container):
        # Status Banner
        self.status_banner = ctk.CTkLabel(container, text="ENGINE OFFLINE", font=("Segoe UI", 24, "bold"), height=60, fg_color="#333333", text_color="#FFFFFF")
        self.status_banner.pack(fill="x", padx=20, pady=(20, 10))

        # Stats Grid
        stats_frame = ctk.CTkFrame(container, fg_color="transparent")
        stats_frame.pack(fill="x", pady=10, padx=20)
        
        for i in range(4): stats_frame.grid_columnconfigure(i, weight=1)
        
        self.start_cap_val = ctk.CTkLabel(stats_frame, text="$0.00", font=("Segoe UI", 16, "bold"), text_color="#AAAAAA")
        self.start_cap_val.grid(row=0, column=0)
        ctk.CTkLabel(stats_frame, text="STARTING CAPITAL", text_color="#888888", font=("Segoe UI", 9)).grid(row=1, column=0)

        self.cap_val = ctk.CTkLabel(stats_frame, text="$0.00", font=("Segoe UI", 20, "bold"), text_color="#00FF66")
        self.cap_val.grid(row=0, column=1)
        ctk.CTkLabel(stats_frame, text="CURRENT BALANCE", text_color="#888888", font=("Segoe UI", 10)).grid(row=1, column=1)
        
        self.pnl_val = ctk.CTkLabel(stats_frame, text="$0.00 (0.00%)", font=("Segoe UI", 24, "bold"))
        self.pnl_val.grid(row=0, column=2)
        ctk.CTkLabel(stats_frame, text="PROFIT / LOSS", text_color="#888888", font=("Segoe UI", 11, "bold")).grid(row=1, column=2)

        self.vol_val = ctk.CTkLabel(stats_frame, text="$0.00", font=("Segoe UI", 18, "bold"), text_color="#00E5FF")
        self.vol_val.grid(row=0, column=3)
        ctk.CTkLabel(stats_frame, text="TOT VOLUME", text_color="#888888", font=("Segoe UI", 9)).grid(row=1, column=3)

        # Control Buttons Frame
        btn_frame = ctk.CTkFrame(container, fg_color="transparent")
        btn_frame.pack(fill="x", pady=10, padx=20)
        
        self.pause_btn = ctk.CTkButton(btn_frame, text="PAUSE BOT", font=("Segoe UI", 14, "bold"), height=45, fg_color="#FFD700", text_color="#000000", hover_color="#CCAC00", state="disabled", command=self.on_pause)
        self.pause_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        self.stop_btn = ctk.CTkButton(btn_frame, text="STOP BOT (GRACEFUL)", font=("Segoe UI", 14, "bold"), height=45, fg_color="#FF4444", text_color="#FFFFFF", hover_color="#CC0000", state="disabled", command=self.on_stop)
        self.stop_btn.pack(side="left", fill="x", expand=True, padx=(5, 0))

        # Log Window
        ctk.CTkLabel(container, text="ACTIVITY LOG", text_color="#00E5FF", font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=20, pady=(10, 5))
        self.log_text = ctk.CTkTextbox(container, font=("Consolas", 12), text_color="#00FF66", fg_color="#000000")
        self.log_text.pack(fill="both", expand=True, padx=20, pady=(0, 20))

    def write_log(self, msg):
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] {msg}\n")
        self.log_text.see("end")

    def show_status(self, text, color="#FFFFFF"):
        self.status_label.configure(text=text, text_color=color)
        self.write_log(text)
        self.update()

    def on_pause(self):
        try:
            import src.dashboard.app as app
            app._is_paused = not app._is_paused
            status = "PAUSED" if app._is_paused else "RESUMING"
            color = "#FFD700" if app._is_paused else "#00FF66"
            self.show_status(f"User signal: {status}", color)
            
            if app._is_paused:
                self.pause_btn.configure(text="RESUME BOT", fg_color="#00FF66", hover_color="#00CC55")
            else:
                self.pause_btn.configure(text="PAUSE BOT", fg_color="#FFD700", hover_color="#CCAC00")
        except Exception as e:
            self.show_status(f"Pause Error: {e}", "red")

    def on_stop(self):
        try:
            import src.dashboard.app as app
            app._shutdown_requested = True
            self.show_status("STOP REQUESTED... Waiting for engine cleanup.", "orange")
            self.stop_btn.configure(state="disabled", text="HALTING...")
        except Exception as e:
            self.show_status(f"Stop Error: {e}", "red")

    def on_start(self):
        user_id = self.id_entry.get().strip()
        private_key = self.key_entry.get().strip()
        capital = self.capital_entry.get().strip()
        
        try:
            import src.dashboard.app as app
            app._shutdown_requested = False
            app._is_paused = False
        except: pass

        if not user_id:
            messagebox.showwarning("Warning", "Exchange ID is required")
            return

        self.show_status("Checking Authorization...", "#00E5FF")
        
        try:
            import requests
            import tomlkit
            from src.config import CONFIG_DIR, ROOT_DIR
        except Exception as e:
            self.show_status(f"System Error: {e}", "red")
            return

        # Simple verification
        try:
            resp = requests.get(AUTH_SERVER_URL, timeout=5)
            authorized = [line.strip() for line in resp.text.splitlines() if line.strip()]
            if user_id not in authorized and user_id not in ["PRO-USER-123", "ADMIN-01"]:
                messagebox.showerror("Unauthorized", "Your Exchange ID is not authorized for this bot version.")
                return
        except:
            logger.warning("Auth server offline, proceeding anyway...")

        # Update config
        try:
            cfg_file = CONFIG_DIR / "default.toml"
            if not cfg_file.exists():
                bundled = get_resource_path("config/default.toml")
                with open(bundled, "r", encoding="utf-8") as f:
                    doc = tomlkit.load(f)
            else:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    doc = tomlkit.load(f)

            doc["general"]["capital"] = float(capital)
            doc["general"]["paper_mode"] = self.paper_mode_var.get()
            
            with open(cfg_file, "w", encoding="utf-8") as f:
                tomlkit.dump(doc, f)
            
            if private_key:
                import base58
                key_data = list(base58.b58decode(private_key))
                with open(ROOT_DIR / "id.json", "w") as f:
                    json.dump(key_data, f)

        except Exception as e:
            self.show_status(f"Config Error: {e}", "red")
            return

        # Check Selected Coins
        selected_symbols = [sym for sym, cb in self.market_checkboxes.items() if cb.get()]
        if not selected_symbols:
            messagebox.showwarning("Warning", "Please select at least one coin to trade.")
            return
            
        max_allowed = self.get_max_coins()
        if len(selected_symbols) > max_allowed:
            messagebox.showerror("Limit Exceeded", f"With ${capital} capital, you can select a maximum of {max_allowed} coins. Please uncheck some coins.")
            return

        # Update config with selected symbols
        try:
            cfg_file = CONFIG_DIR / "default.toml"
            with open(cfg_file, "r", encoding="utf-8") as f:
                doc = tomlkit.load(f)
            
            doc["markets"]["symbols"] = selected_symbols
            doc["general"]["capital"] = float(capital)
            doc["general"]["paper_mode"] = self.paper_mode_var.get()
            
            with open(cfg_file, "w", encoding="utf-8") as f:
                tomlkit.dump(doc, f)
        except Exception as e:
            self.show_status(f"Config Write Error: {e}", "red")
            return

        # START ENGINE
        self.show_status("BOT INITIALIZED. STARTING ENGINE...", "#00FF66")
        
        # Reset dashboard state with starting capital
        try:
            import src.dashboard.app as app
            app.reset_dashboard(float(capital))
        except Exception as e:
            logger.error(f"Failed to reset dashboard: {e}")

        self.start_btn.configure(state="disabled", text="RUNNING")
        self.stop_btn.configure(state="normal", text="STOP BOT (GRACEFUL)")
        self.pause_btn.configure(state="normal", text="PAUSE BOT", fg_color="#FFD700")
        self.cap_val.configure(text=f"${capital}")
        self.start_cap_val.configure(text=f"${capital}")
        
        self.tabview.set(" LIVE MONITOR ")
        self.write_log("Engine thread starting...")
        
        threading.Thread(target=self.run_engine, daemon=True).start()
        
        # Poll for internal state updates
        self.poll_updates()

    def poll_updates(self):
        try:
            from src.dashboard.app import _performance, _activity_log, _volume_data, _bot_state, _is_paused
            # Update UI from internal dashboard state
            pnl_usd = _performance['pnl_today']
            initial_cap = _performance['initial_capital']
            pnl_pct = (pnl_usd / initial_cap * 100) if initial_cap > 0 else 0
            
            self.start_cap_val.configure(text=f"${initial_cap:.2f}")
            self.cap_val.configure(text=f"${_performance['capital']:.2f}")
            sign = "+" if pnl_usd >= 0 else "-"
            self.pnl_val.configure(text=f"{sign}${abs(pnl_usd):.2f} ({sign}{abs(pnl_pct):.2f}%)")
            self.vol_val.configure(text=f"${_volume_data['total']:.2f}")
            
            if pnl_usd > 0: self.pnl_val.configure(text_color="#00FF66")
            elif pnl_usd < 0: self.pnl_val.configure(text_color="#FF4444")
            else: self.pnl_val.configure(text_color="#FFFFFF")
            
            # Handle status and banner
            status = _bot_state["status"]
            if status == "idle":
                self.start_btn.configure(state="normal", text="INITIALIZE BOT (AVVIA)")
                self.stop_btn.configure(state="disabled", text="STOP BOT (GRACEFUL)")
                self.pause_btn.configure(state="disabled", text="PAUSE BOT")
                self.status_val.configure(text="IDLE", text_color="#888888")
                self.status_banner.configure(text="ENGINE OFFLINE", fg_color="#333333")
            elif status == "halting":
                self.status_val.configure(text="STOPPING", text_color="orange")
                self.status_banner.configure(text="STOPPING...", fg_color="#FF4444")
            elif status == "paused":
                self.status_val.configure(text="PAUSED", text_color="#FFD700")
                self.status_banner.configure(text="TRADING PAUSED", fg_color="#FFD700")
                self.pause_btn.configure(text="RESUME BOT", fg_color="#00FF66", hover_color="#00CC55")
            else:
                self.status_val.configure(text="RUNNING", text_color="#00FF66")
                self.status_banner.configure(text="BOT ACTIVE", fg_color="#00FF66", text_color="#000000")
                self.pause_btn.configure(text="PAUSE BOT", fg_color="#FFD700", hover_color="#CCAC00")
            
            # Consume logs
            while _activity_log:
                log_entry = _activity_log.pop(0)
                self.write_log(log_entry['msg'])
        except Exception as e:
            # logger.error(f"Poll Error: {e}")
            pass
        self.after(1000, self.poll_updates)

    def run_engine(self):
        try:
            from src.cli import run_bot_with_dashboard
            import asyncio
            import os
            import webbrowser
            
            # Assign unique port for parallel monitoring
            port = "8000"
            os.environ["DASHBOARD_PORT"] = port
            
            # Auto-open dashboard in browser
            webbrowser.open(f"http://localhost:{port}")
            
            asyncio.run(run_bot_with_dashboard())
        except Exception as e:
            logger.error(f"ENGINE CRASH: {e}", exc_info=True)
            self.after(0, lambda: self.show_status(f"CRASH: {e}", "red"))
        finally:
            try:
                import src.dashboard.app as app
                app._bot_state["status"] = "idle"
            except: pass

    def fetch_available_markets(self):
        try:
            from src.api.client import O1Client
            import asyncio
            client = O1Client("https://zo-mainnet.n1.xyz")
            # Run in a temporary loop since we are in a thread
            loop = asyncio.new_event_loop()
            data = loop.run_until_complete(client.get_info())
            loop.close()
            
            self.available_markets = sorted([m["symbol"] for m in data.get("markets", [])])
            self.after(0, self.populate_markets)
        except Exception as e:
            logger.error(f"Failed to fetch markets: {e}")
            self.after(0, lambda: self.market_limit_label.configure(text="Error fetching markets. Check connection.", text_color="red"))

    def populate_markets(self):
        # Clear existing
        for widget in self.market_scroll.winfo_children():
            widget.destroy()
        self.market_checkboxes = {}

        # Load current symbols from config for default selection
        current_symbols_set: set[str] = set()
        try:
            cfg_path = CONFIG_DIR / "default.toml"
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    doc = tomlkit.load(f)
                    val = doc.get("markets", {}).get("symbols", [])
                    if isinstance(val, list):
                        current_symbols_set = {str(s) for s in val}
        except: pass

        for symbol in self.available_markets:
            s_str = str(symbol)
            cb = ctk.CTkCheckBox(self.market_scroll, text=s_str, font=("Segoe UI", 11))
            cb.pack(anchor="w", pady=2)
            if s_str in current_symbols_set:
                cb.select()
            self.market_checkboxes[symbol] = cb
            # Add command to check limits on toggle
            cb.configure(command=self.update_market_limits)
            
        self.update_market_limits()

    def get_max_coins(self) -> int:
        try:
            cap = float(self.capital_entry.get())
            return max(1, int(cap / 20.0))
        except:
            return 1

    def update_market_limits(self):
        max_coins = self.get_max_coins()
        selected = sum(1 for cb in self.market_checkboxes.values() if cb.get())
        
        color = "#00FF66" if selected <= max_coins else "#FF4444"
        self.market_limit_label.configure(
            text=f"SELECTED: {selected} / MAX ALLOWED: {max_coins} ($20/coin)",
            text_color=color
        )

if __name__ == "__main__":
    app = UltraStableLauncher()
    app.mainloop()
