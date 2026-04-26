import {useState} from "react";

/**
 * Onboarding card explaining the build/verify/merge loop and rough cost.
 * Dismissed via localStorage so returning users don't see it again.
 * mc-audit redesign §7 W7.1.
 */
export function LauncherExplainer() {
  const KEY = "otto.launcher.explainer.dismissed";
  const [dismissed, setDismissed] = useState<boolean>(() => {
    try { return window.localStorage.getItem(KEY) === "1"; } catch { return false; }
  });
  if (dismissed) return null;
  const onDismiss = () => {
    try { window.localStorage.setItem(KEY, "1"); } catch { /* ignore quota errors */ }
    setDismissed(true);
  };
  return (
    <aside className="launcher-explainer" data-testid="launcher-explainer" aria-label="How Otto works">
      <div className="launcher-explainer-body">
        <strong>How Otto works.</strong>
        <p>
          Queue a job → Otto runs it in an isolated git worktree → review the result and merge.
          A small feature typically takes <strong>5–15 min</strong> and uses <strong>~$0.50–$2</strong> in tokens.
        </p>
      </div>
      <button type="button" className="launcher-explainer-dismiss" onClick={onDismiss} aria-label="Hide explainer">×</button>
    </aside>
  );
}
