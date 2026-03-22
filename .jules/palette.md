## 2025-02-28 - Avoid Inline Styles for Interactive Elements
**Learning:** Using inline styles (`style="..."`) on interactive elements like buttons prevents the use of CSS pseudo-classes (`:hover`, `:focus-visible`, `:disabled`). This degrades the user experience by removing visual feedback for interactions and keyboard navigation.
**Action:** Always define button styles in external CSS and use semantic classes (e.g., `.btn`, `.btn-primary`) to ensure consistent behavior and accessibility states.

## 2024-03-22 - [Add graceful DOM updates]
**Learning:**
When updating dynamic content on a dashboard, manipulating DOM elements directly without null checks can cause uncaught TypeErrors, halting the entire UI update loop if a single element is missing (e.g. from a removed feature or stripped-down template).
**Action:**
Introduced robust DOM manipulation helpers (`setText`, `setClass`, `setHTML`, `setWidth`) that safely verify element existence before applying updates. Additionally, embedded a subtle pulse animation within the `setText` helper to highlight value changes, gently drawing the user's attention to real-time updates without overwhelming the interface.
