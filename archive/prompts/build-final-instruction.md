**Final instruction**: Build only the tasks under `## What you must do`
above, writing ONLY to paths under `## Scope`. If a whole-product feature in
the context block isn't in your tasks, another group will deliver it. When your
group's tasks are done and your acceptance checks pass, confirm completion.

Before you confirm completion, if you created or edited any scaffold-critical
file (`start.sh`, `package.json`, `tsconfig*`, `vite.config.*`, backend
manifest, ORM base, store), you MUST pass the **SCAFFOLD CONFORMANCE
SELF-CHECK** in the framework-conventions section above (pinned majors,
`start.sh` honors the injected `$PORT` + `--strictPort` + `python3`/venv,
build script `tsc -b && vite build`). A scaffold that fails it is not done —
it will fail clean-boot and force an expensive re-dispatch.
