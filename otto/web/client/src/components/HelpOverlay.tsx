/**
 * HelpOverlay — keyboard shortcut cheat sheet, opens with `?`.
 * mc-audit redesign Phase F.
 */
export function HelpOverlay({onClose}: {onClose: () => void}) {
  const shortcuts: Array<{keys: string[]; label: string}> = [
    {keys: ["n"], label: "New job"},
    {keys: ["⌘", "K"], label: "Command palette / project picker"},
    {keys: ["j"], label: "Next task"},
    {keys: ["k"], label: "Previous task"},
    {keys: ["1"], label: "Inspector: Try product"},
    {keys: ["2"], label: "Inspector: Result"},
    {keys: ["3"], label: "Inspector: Code changes"},
    {keys: ["4"], label: "Inspector: Logs"},
    {keys: ["5"], label: "Inspector: Artifacts"},
    {keys: ["?"], label: "Show this help"},
    {keys: ["Esc"], label: "Close drawer / dialog"},
  ];
  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="help-overlay"
        role="dialog"
        aria-modal="true"
        aria-labelledby="helpOverlayHeading"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="help-overlay-head">
          <h2 id="helpOverlayHeading">Keyboard shortcuts</h2>
          <button type="button" aria-label="Close" onClick={onClose}>×</button>
        </div>
        <ul className="help-overlay-list">
          {shortcuts.map((row) => (
            <li key={row.keys.join("+")}>
              <span>{row.label}</span>
              <span className="help-overlay-keys">
                {row.keys.map((key, i) => (
                  <kbd key={`${key}-${i}`}>{key}</kbd>
                ))}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
