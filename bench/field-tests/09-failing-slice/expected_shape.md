# Expected Shape

Forced tier: `modular`. The driver passes `--tier modular`.

This scenario validates FAILURE PROPAGATION, not a clean tree. Root
emits scaffold + four subsystem leaves: core API, web UI, vault
export, reporting (reporting `depends_on` vault export).

Expected honest behavior under the post-hardening code:

- Core API leaf and Web UI leaf: `pass`, branches reach `main`.
- Vault export leaf: fails honestly (the impossible SDK). It must
  NOT be canonicalized to a false `pass`, must NOT be silently
  merged. It should end `merge_blocked`/`catastrophic`/non-pass with
  the real reason in its failure detail.
- Reporting leaf: its dependency (export) never reached a
  dependency-satisfying verdict, so per the Pass-4 fix it must stay
  `waiting_on_deps` and never be dispatched/merged as if export
  succeeded.
- Root: an HONEST non-pass overall verdict (partial or
  merge_blocked). The working slices (core, UI) still land on
  `main`. No infinite retry loop, no silent broken merge, no hang —
  the run terminates with a loud structural reason.

It would be a FAILURE of this scenario if:
- the export slice is reported `pass` (fabricated success), or its
  broken code is merged to `main`;
- the reporting slice runs/merges despite its failed dependency;
- the overall verdict is `pass` (dishonest);
- the run hangs / loops instead of terminating with a clear reason;
- the working core/UI slices are lost because one sibling failed.

This is the counterpart to the recursion scenarios: recursion tests
the happy nested path; this tests that the hardened merge/verdict/
dependency gates fail loud and contained.
