## 2025-02-28 - Avoid Inline Styles for Interactive Elements
**Learning:** Using inline styles (`style="..."`) on interactive elements like buttons prevents the use of CSS pseudo-classes (`:hover`, `:focus-visible`, `:disabled`). This degrades the user experience by removing visual feedback for interactions and keyboard navigation.
**Action:** Always define button styles in external CSS and use semantic classes (e.g., `.btn`, `.btn-primary`) to ensure consistent behavior and accessibility states.
