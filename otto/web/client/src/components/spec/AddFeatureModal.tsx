// AddFeatureModal — convenience UI for appending a new Feature to the spec
// during review (wireframes 4c).
//
// The user types a name + description + optionally picks an existing Group.
// On submit the modal mutates the parent's draft markdown by appending a
// new "#### <name>" feature block with `<!-- feature: <slug> -->` metadata.
// The modal does NOT post to the backend itself — the parent's existing
// Save handler does that on the next user click.
//
// Slug rules: lowercase, dashes-only, deduped against the parent's existing
// feature ids. The slug is derived from the name, but the user can override.

import { useEffect, useMemo, useRef, useState } from "react";

interface Props {
  /** Existing Group ids in the current draft, for the Group dropdown. */
  groupIds: string[];
  /** Existing Feature ids in the current draft, for slug-uniqueness check. */
  existingFeatureIds: string[];
  /**
   * Called with a Feature block to append to the markdown. Includes
   * leading newlines so the parent can concatenate without further
   * formatting. Empty group_id means "Ungrouped".
   */
  onAppend: (block: string) => void;
  onClose: () => void;
}

function slugify(name: string): string {
  const lower = name.trim().toLowerCase();
  // Replace any run of non-[a-z0-9] with a single dash; trim dashes.
  const slug = lower.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return slug || "feature";
}

function uniqueSlug(seed: string, taken: Set<string>): string {
  if (!taken.has(seed)) return seed;
  let n = 2;
  while (taken.has(`${seed}-${n}`)) n += 1;
  return `${seed}-${n}`;
}

function buildFeatureBlock(args: {
  groupId: string;
  featureId: string;
  name: string;
  description: string;
  evidenceKinds: string;
  acceptance: string;
}): string {
  const lines: string[] = [];
  if (args.groupId) {
    // The parent's markdown already has Group sections; we append into
    // a free-form trailing block under the chosen Group. Insertion at
    // the end is honest — the parent can move it on subsequent edits.
    lines.push("");
    lines.push(`### ${args.groupId} (continued)`);
    lines.push(`<!-- group: ${args.groupId} -->`);
  } else {
    lines.push("");
    lines.push("### Ungrouped");
  }
  lines.push("");
  lines.push(`#### ${args.name}`);
  const ev = args.evidenceKinds.trim();
  lines.push(
    ev
      ? `<!-- feature: ${args.featureId} | evidence: ${ev} -->`
      : `<!-- feature: ${args.featureId} -->`,
  );
  if (args.description.trim()) {
    lines.push("");
    lines.push(args.description.trim());
  }
  if (args.acceptance.trim()) {
    lines.push("");
    lines.push(`**Acceptance:** ${args.acceptance.trim()}`);
  }
  return lines.join("\n") + "\n";
}

export function AddFeatureModal({
  groupIds,
  existingFeatureIds,
  onAppend,
  onClose,
}: Props) {
  const [name, setName] = useState<string>("");
  const [description, setDescription] = useState<string>("");
  const [acceptance, setAcceptance] = useState<string>("");
  const [evidenceKinds, setEvidenceKinds] = useState<string>("");
  const [groupId, setGroupId] = useState<string>(groupIds[0] ?? "");
  const [featureIdOverride, setFeatureIdOverride] = useState<string>("");
  const nameInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    nameInputRef.current?.focus();
  }, []);

  const taken = useMemo(
    () => new Set(existingFeatureIds),
    [existingFeatureIds],
  );
  const computedFeatureId = useMemo(() => {
    if (featureIdOverride.trim()) return featureIdOverride.trim();
    return uniqueSlug(slugify(name), taken);
  }, [name, featureIdOverride, taken]);

  const idCollision = featureIdOverride.trim() && taken.has(computedFeatureId);
  const canSubmit =
    name.trim().length > 0 && computedFeatureId.length > 0 && !idCollision;

  function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!canSubmit) return;
    const block = buildFeatureBlock({
      groupId,
      featureId: computedFeatureId,
      name: name.trim(),
      description,
      evidenceKinds,
      acceptance,
    });
    onAppend(block);
    onClose();
  }

  return (
    <div
      className="modal-backdrop"
      data-testid="add-feature-backdrop"
      onClick={(e) => {
        // Click outside the dialog closes the modal; clicks inside don't.
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <form
        className="add-feature-modal"
        data-testid="add-feature-modal"
        onSubmit={handleSubmit}
      >
        <header className="add-feature-modal-header">
          <h2>Add Feature</h2>
          <button
            type="button"
            className="modal-close"
            data-testid="add-feature-close"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </header>

        <label>
          <span>Name</span>
          <input
            ref={nameInputRef}
            type="text"
            data-testid="add-feature-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Markdown rendering"
            required
          />
        </label>

        <label>
          <span>Feature id</span>
          <input
            type="text"
            data-testid="add-feature-id"
            value={featureIdOverride || computedFeatureId}
            onChange={(e) => setFeatureIdOverride(e.target.value)}
            placeholder="auto-derived from name"
          />
          {idCollision && (
            <span
              className="field-error"
              data-testid="add-feature-id-error"
              role="alert"
            >
              Id "{computedFeatureId}" is already in use.
            </span>
          )}
        </label>

        <label>
          <span>Group</span>
          <select
            data-testid="add-feature-group"
            value={groupId}
            onChange={(e) => setGroupId(e.target.value)}
          >
            <option value="">— Ungrouped —</option>
            {groupIds.map((g) => (
              <option key={g} value={g}>
                {g}
              </option>
            ))}
          </select>
        </label>

        <label>
          <span>Description</span>
          <textarea
            data-testid="add-feature-description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
            placeholder="What does this feature do?"
          />
        </label>

        <label>
          <span>Evidence kinds (comma-separated)</span>
          <input
            type="text"
            data-testid="add-feature-evidence"
            value={evidenceKinds}
            onChange={(e) => setEvidenceKinds(e.target.value)}
            placeholder="e.g. BrowserJourney, RepoTestCheck"
          />
        </label>

        <label>
          <span>Acceptance</span>
          <textarea
            data-testid="add-feature-acceptance"
            value={acceptance}
            onChange={(e) => setAcceptance(e.target.value)}
            rows={2}
            placeholder="Concrete pass/fail criterion (optional)"
          />
        </label>

        <footer className="add-feature-modal-actions">
          <button
            type="button"
            data-testid="add-feature-cancel"
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            type="submit"
            data-testid="add-feature-submit"
            disabled={!canSubmit}
          >
            Add to spec
          </button>
        </footer>
      </form>
    </div>
  );
}

export default AddFeatureModal;
