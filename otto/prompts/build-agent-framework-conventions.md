**Framework conventions — MANDATORY pinned contract.**

You are building on a fixed, mutually-verified stack. Guessing framework
versions or APIs is the single largest cause of scaffolds that will not
strict-`tsc`-build or boot. This section removes the guessing.

**SCAFFOLD CONFORMANCE SELF-CHECK — run this BEFORE you confirm completion.**
If you created or edited any of `start.sh`, `package.json`, `tsconfig*.json`,
`vite.config.*`, `pyproject.toml`/`requirements.txt`, the ORM base, or the
zustand store, every line below must be TRUE for the files you actually wrote.
A "no" is a defect you must fix before returning — these are exactly the
deviations that fail clean-boot and force an expensive re-dispatch:

- `start.sh` binds the backend to the otto-injected port: it reads
  `${PORT:-...}` (NOT a bespoke `BACKEND_PORT`/hard-coded port) and the dev
  server uses `--strictPort` (never silently drifts 5173→5174).
- `start.sh` invokes Python as `python3`/a venv interpreter (NEVER bare
  `python` — it is not on PATH; `python: command not found` = this defect)
  and installs deps hermetically per the ports/start.sh section.
- `package.json` pins the EXACT majors from *Canonical Manifests* (e.g.
  Vite `^6`, not `^5`); the build script is `tsc -b && vite build` (NOT
  `tsc --noEmit && vite build`); no `latest`/`*`/invented versions.
- Every scaffold-critical file matches its template below except the marked
  `<<PRODUCT_SLOT: ...>>` regions; no framework substituted/up/down-graded.

If any answer is "no", you have not met the contract — fix it, do not return.

Rules:

- For **scaffold-critical files** (every config/manifest/skeleton with a
  fenced template below) reproduce the template **verbatim**, changing only
  the explicitly marked `<<PRODUCT_SLOT: ...>>` regions. Do not "improve",
  reorder, or restyle them.
- Use **exactly** the versions in *Canonical Manifests* below. Never write
  `"latest"`, `"^latest"`, `"*"`, or an invented version. Per-framework
  sections show patterns; *Canonical Manifests* owns the version numbers.
- Do not substitute, upgrade, or downgrade a framework (no React 19, no
  Tailwind v4, no SQLAlchemy 1.x, no react-router 7, no pytest 9, no
  Pydantic v1, no TanStack Query v4 positional API).
- If the product genuinely cannot be built on this stack, state precisely
  why in `decisions.md` before diverging — do not silently diverge.
- These conventions are authoritative over any stale memory you have about
  these libraries; they were registry/PyPI-verified for this stack.

---

# Canonical Manifests (single source of truth for ALL versions)

Every per-framework section defers to these exact pins. Reconciled across
all audits; do not contradict them elsewhere.

**Frontend `package.json`** (React 18.3 + Vite 6 + TS 5.7 strict; add
zustand/router/query/tailwind only if the product uses them):

```json
{
  "name": "<<PRODUCT_SLOT: package name>>",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "preview": "vite preview --host 127.0.0.1 --strictPort",
    "test": "vitest run"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "zustand": "^5.0.13",
    "react-router-dom": "^6.30.3",
    "@tanstack/react-query": "^5.100.11"
  },
  "devDependencies": {
    "@types/node": "^22.10.2",
    "@types/react": "^18.3.18",
    "@types/react-dom": "^18.3.5",
    "@vitejs/plugin-react": "^4.3.4",
    "typescript": "~5.7.2",
    "vite": "^6.0.5",
    "vitest": "^2.1.8",
    "@testing-library/react": "^16.1.0",
    "@testing-library/jest-dom": "^6.6.3",
    "jsdom": "^25.0.1",
    "tailwindcss": "3.4.19",
    "postcss": "8.5.14",
    "autoprefixer": "10.5.0"
  }
}
```

`typescript` uses `~5.7.2` (pin minor: 5.8 introduces `erasableSyntaxOnly`
these tsconfigs intentionally omit). `react-router-dom` floating `latest`
on npm is v7 — the `^6.30.3` pin keeps you on v6.

**Backend `pyproject.toml`** (Python 3.12, FastAPI 0.115, SQLAlchemy 2.0,
Pydantic v2):

```toml
[project]
name = "<<PRODUCT_SLOT: backend pkg>>"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115,<0.116",
  "uvicorn[standard]>=0.34,<0.35",
  "SQLAlchemy>=2.0,<2.1",
  "pydantic>=2.13,<3",
  "pydantic-settings>=2.14,<3",
  "alembic>=1.13,<1.14",
]
[project.optional-dependencies]
dev = ["pytest>=8.4,<9", "httpx>=0.27"]
```

(`pytest-asyncio>=0.24` only if the product has `async def test_*`.
SQLAlchemy `<2.1` is deliberate — 2.1.0b1 exists and is not 2.0.)

---

# Vite 6 + TypeScript 5.7 strict (pinned)

Fixes the recurring `vite.config.ts ... error TS2580: Cannot find name
'process'`: `@types/node` devDep + a node-scoped tsconfig that owns
`vite.config.ts`. The app tsconfig stays Node-free.

`tsconfig.json` (root — references only):

```json
{
  "files": [],
  "references": [
    { "path": "./tsconfig.app.json" },
    { "path": "./tsconfig.node.json" }
  ]
}
```

`tsconfig.app.json` (strict, `src` only — browser; NO `node` types here):

```json
{
  "compilerOptions": {
    "tsBuildInfoFile": "./node_modules/.tmp/tsconfig.app.tsbuildinfo",
    "target": "ES2022",
    "useDefineForClassFields": true,
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "isolatedModules": true,
    "moduleDetection": "force",
    "noEmit": true,
    "jsx": "react-jsx",
    "verbatimModuleSyntax": true,
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "noUncheckedSideEffectImports": true,
    "types": ["vite/client"]
  },
  "include": ["src"]
}
```

`tsconfig.node.json` (node-scoped — owns `vite.config.ts`; `"types":
["node"]` is what kills TS2580):

```json
{
  "compilerOptions": {
    "tsBuildInfoFile": "./node_modules/.tmp/tsconfig.node.tsbuildinfo",
    "target": "ES2022",
    "lib": ["ES2023"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "isolatedModules": true,
    "moduleDetection": "force",
    "noEmit": true,
    "composite": true,
    "verbatimModuleSyntax": true,
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "types": ["node"]
  },
  "include": ["vite.config.ts"<<PRODUCT_SLOT: ,"playwright.config.ts" if E2E>>]
}
```

`vite.config.ts` (strict-tsc-clean; `process` is legal here because this
file is owned by `tsconfig.node.json`):

```typescript
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: Number(process.env.VITE_PORT) || <<PRODUCT_SLOT: dev port, e.g. 5173>>,
    strictPort: true,
    proxy: {
      '/api': {
        target: process.env.VITE_API_URL || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
```

Idioms:
- Client code (`src/**`) reads env via `import.meta.env.VITE_FOO` — NEVER
  `process.env` (TS2580 + undefined at runtime; only `VITE_`-prefixed vars
  are exposed).
- `process`/`__dirname`/`path` are legal ONLY in files matched by
  `tsconfig.node.json`. Do NOT add `"types":["node"]` or `@types/node` to
  `tsconfig.app.json` (it masks real client TS2580s).
- `verbatimModuleSyntax: true` → type-only imports MUST be `import type`.
- `tsconfig.node.json` MUST have `composite: true` (project references +
  `tsc -b`), else TS6306.
- Never emit/commit `vite.config.js`, `*.config.js`, or generated `*.d.ts`
  next to the `.ts` configs (duplicate ambient decls → TS2300/TS6200).
- Build script is `tsc -b && vite build`.

# React 18.3 (pinned)

`index.html` (Vite 6, project root):

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title><<PRODUCT_SLOT: product name>></title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

`src/main.tsx` (no non-null assertion — narrow explicitly so strict +
`no-non-null-assertion` stay clean; QueryClient/Router providers added by
their sections):

```tsx
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './index.css'

const container = document.getElementById('root')
if (!container) throw new Error('Root element #root not found')

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
```

`src/App.tsx` (minimal typed shell):

```tsx
import { useState } from 'react'

export default function App(): JSX.Element {
  const [count, setCount] = useState<number>(0)
  return (
    <main>
      <h1><<PRODUCT_SLOT: product name>></h1>
      <button type="button" onClick={() => setCount((c) => c + 1)}>
        count: {count}
      </button>
      {/* <<PRODUCT_SLOT: app content>> */}
    </main>
  )
}
```

Idioms:
- StrictMode double-invokes render/effects/updaters in dev only — keep
  effects idempotent, updaters pure.
- Type components as `function Name(props: Props): JSX.Element`; avoid
  `React.FC`. Children: `React.ReactNode`. Hooks: `useState<T>()`,
  `useRef<HTMLInputElement>(null)` then null-check.
- Events: `React.ChangeEvent<HTMLInputElement>`,
  `React.FormEvent<HTMLFormElement>`. Always `<button type="button">`
  unless it submits.
- No `any` — `unknown` + narrowing. `"jsx":"react-jsx"` → no `import React`
  needed; import named hooks only.
- Do NOT use React 19 APIs on 18.3: `use()`, `useActionState`,
  `useFormStatus`, `useOptimistic`, ref-as-prop, `<Context>` as provider
  (use `<Context.Provider>`). `@types/react@18.3` rejects them.

# zustand 5 (pinned)

`src/store/index.ts` — fixes the recurring `TS2345 ... type 'never'`
caused by the non-curried/un-generic `create`. The curried
`create<T>()(...)` form is **mandatory**.

```ts
import { create } from 'zustand'
import { devtools, persist } from 'zustand/middleware'
import { useShallow } from 'zustand/shallow'

interface State {
  // <<PRODUCT_SLOT: state fields, e.g. items: Item[]>>
  count: number
}

interface Actions {
  // <<PRODUCT_SLOT: action signatures>>
  increment: (by: number) => void
  reset: () => void
}

export type AppStore = State & Actions

const initialState: State = {
  // <<PRODUCT_SLOT: initial values for every State field>>
  count: 0,
}

// v5 REQUIRED curried form: create<T>()(...). devtools OUTERMOST.
export const useAppStore = create<AppStore>()(
  devtools(
    persist(
      (set, get) => ({
        ...initialState,
        // <<PRODUCT_SLOT: action implementations>>
        increment: (by) => set((s) => ({ count: s.count + by }), false, 'increment'),
        reset: () => set(initialState, false, 'reset'),
      }),
      {
        name: '<<PRODUCT_SLOT: persisted key>>',
        partialize: (s): Pick<State, 'count'> => ({ count: s.count }),
      },
    ),
    { name: 'AppStore' },
  ),
)

// single primitive selector — no useShallow needed
export const useCount = () => useAppStore((s) => s.count)
// multi-field/object/array selector MUST wrap in useShallow (v5 infinite-loop footgun)
export const useAppActions = () =>
  useAppStore(useShallow((s) => ({ increment: s.increment, reset: s.reset })))
```

Idioms / v4→v5 footguns:
- **`create<T>()((set,get)=>({...}))` is MANDATORY** — the extra `()` plus
  explicit `<T>`. Non-curried `create<T>(impl)` or untyped `create(impl)`
  infers `set` as `never` → the exact `TS2345 ... 'never'` error. This is
  the single fix.
- `set` is `(partial, replace?, action?)`. Full replace needs a COMPLETE
  state object: `set(full, true)`; `set({}, true)` is a type error.
- A selector returning a NEW object/array each call → infinite re-renders;
  wrap with `useShallow` (from `zustand/shallow`).
- `devtools` OUTERMOST: `devtools(persist(...))`.
- No default import: `import { create } from 'zustand'`.

# react-router-dom 6 (pinned)

Data-router setup (preferred over `<BrowserRouter>`):

```tsx
// src/router.tsx
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
// wire into main.tsx: <RouterProvider router={router} />
export const router = createBrowserRouter([
  {
    path: '/',
    element: <RootLayout />,        // renders <Outlet/>
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Home /> },
      { path: '<<PRODUCT_SLOT: items>>/:id', element: <Detail /> },
    ],
  },
])
```

Idioms (v6.x):
- v5→v6: `Switch`→`Routes`; `component=`/`render=`→`element={<X/>}`;
  `useHistory`→`useNavigate`; `Redirect`→`<Navigate to=".." replace/>`;
  nested paths relative; exact matching default.
- Do NOT use v7/framework-mode APIs: import everything from
  `react-router-dom` (not bare `react-router`); no `app/routes/`, no
  `<Scripts/>`, no `useLoaderData<typeof loader>()` (v7-only).
- `useParams<'id'>()` → values are `string | undefined`; narrow before
  use. `useNavigate()` is typed (`NavigateFunction`), no generic.
- Parent `element` must render `<Outlet/>`; `{ index: true }` for default
  child. `useLoaderData` on v6 has no auto typing — annotate
  `as <<PRODUCT_SLOT: DTO>>`.
- Types ship with the lib (no `@types/react-router-dom`).

# @tanstack/react-query 5 (pinned)

```tsx
// src/lib/queryClient.ts
import { QueryClient } from '@tanstack/react-query'
export const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, gcTime: 5 * 60_000, retry: 1 } },
})
// main.tsx: <QueryClientProvider client={queryClient}><App/></QueryClientProvider>
```

```tsx
import { useQuery, useMutation, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

const keys = { all: ['<<PRODUCT_SLOT: resource>>'] as const,
  detail: (id: string) => ['<<PRODUCT_SLOT: resource>>', id] as const }

function useItem(id: string): UseQueryResult<Item, Error> {
  return useQuery({
    queryKey: keys.detail(id),
    queryFn: async ({ signal }): Promise<Item> => {
      const r = await fetch(`/api/items/${id}`, { signal })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json() as Promise<Item>
    },
    enabled: id !== '',
  })
}
```

Idioms / v4→v5 footguns:
- Hooks are **object-arg only**: `useQuery({ queryKey, queryFn })`. The
  positional `useQuery(key, fn)` form was removed in v5.
- `isLoading`→**`isPending`** for first-load gating; `cacheTime`→**`gcTime`**.
- `useQuery` no longer takes `onSuccess`/`onError` (still valid on
  `useMutation`); handle via returned `error`/`isError`.
- Throw from `queryFn`/`mutationFn`; type `useMutation<TData,Error,TVars>`.
- `keepPreviousData` → `placeholderData: keepPreviousData`. `Hydrate`→
  `HydrationBoundary`. Define keys as `as const` tuples.

# tailwindcss 3.4 (pinned)

`tailwind.config.js` (project root, verbatim):

```js
/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: { extend: {} },
  plugins: [],
}
```

`postcss.config.js` (project root, verbatim):

```js
export default { plugins: { tailwindcss: {}, autoprefixer: {} } }
```

`src/index.css` (these three directives MUST be the first rules; imported
by `main.tsx` via `import './index.css'`):

```css
@tailwind base;
@tailwind components;
@tailwind utilities;
```

Idioms:
- v3 is config-file driven. Do NOT emit v4 CSS-first `@theme {}` /
  `@import "tailwindcss"` / `@tailwindcss/vite` / `@tailwindcss/postcss`
  (v4-only; breaks v3).
- v3 plugs into Vite through PostCSS, not a Vite plugin.
- Content globs are purge-critical: a class in an unmatched file is
  stripped from prod CSS. No dynamic class strings (`` `text-${c}-500` ``).
- Config files are ESM (`export default`) because `"type":"module"`.

# FastAPI 0.115 (pinned)

`backend/main.py` (verbatim skeleton — features add routers, never edit
this file's body):

```python
import importlib
import os
import pkgutil
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # <<PRODUCT_SLOT: startup (e.g. Base.metadata.create_all); teardown after yield>>
    yield


app = FastAPI(title="<<PRODUCT_SLOT: product name>>", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _include_feature_routers() -> None:
    import routers  # backend/routers/__init__.py (empty file is fine)

    for mod in pkgutil.iter_modules(routers.__path__):
        module = importlib.import_module(f"routers.{mod.name}")
        candidate = getattr(module, "router", None)
        if isinstance(candidate, APIRouter):
            app.include_router(candidate, prefix="/api")


_include_feature_routers()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        reload=bool(os.environ.get("DEV")),
    )
```

Feature router (`backend/routers/<<PRODUCT_SLOT: feature>>.py`):

```python
from fastapi import APIRouter

router = APIRouter(prefix="/<<PRODUCT_SLOT: resource>>", tags=["<<PRODUCT_SLOT: feature>>"])
```

Idioms:
- `lifespan=` async context manager only — never `@app.on_event` (silently
  ignored when `lifespan=` is set).
- Each feature = one `backend/routers/<feature>.py` exporting `router`;
  auto-discovery mounts it under `/api`. Features MUST NOT touch
  `main.py` (kills cross-feature router merge conflicts).
- Pydantic v2 models; `model_config = ConfigDict(from_attributes=True)` for
  ORM serialization; `return orm_obj` with `response_model=`.
- DB session via `Depends(get_db)`. `allow_credentials=True` forbids
  `allow_origins=["*"]` — keep the explicit Vite-origin list.
- `uvicorn[standard]` (not bare `uvicorn`).

# SQLAlchemy 2.0 (pinned) — single Base invariant (fix-11)

`backend/db/base.py` (THE one Base / engine / sessionmaker / get_db,
verbatim):

```python
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = "sqlite:///./<<PRODUCT_SLOT: db name>>.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """THE single declarative Base for the WHOLE product. Every feature
    model in every module MUST `from backend.db.base import Base` and
    subclass THIS. Never declarative_base(), never a 2nd DeclarativeBase,
    never a 2nd MetaData() — two Bases => duplicate __tablename__ and
    orphan cross-feature FKs."""


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

Feature model (`backend/<<PRODUCT_SLOT: feature>>/models.py`):

```python
from typing import Optional

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base  # the shared Base. Never redefine.


class User(Base):
    __tablename__ = "users"  # unique across the ENTIRE product
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(120))
    items: Mapped[list["Item"]] = relationship(back_populates="owner")


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    owner: Mapped["User"] = relationship(back_populates="items")
```

Idioms — 1.x→2.0 footguns:
- `class Base(DeclarativeBase): pass` — NEVER `declarative_base()`.
- Typed columns `Mapped[int] = mapped_column(...)`; `Optional[X]` infers
  `nullable=True`. No bare `Column(...)` without `Mapped[]`.
- Queries: 2.0 `select()` only — `db.scalars(select(M)).all()`. No
  `db.query(M)` (1.x legacy).
- SQLite engine MUST pass `connect_args={"check_same_thread": False}`;
  `expire_on_commit=False` avoids `DetachedInstanceError` on response
  serialization.
- **One `DeclarativeBase` for the whole product** (fix-11). Cross-feature
  FK works only because all models share one `Base.metadata`.

# Pydantic v2 (pinned)

```python
# schemas.py
from pydantic import BaseModel, ConfigDict, field_validator

class ItemCreate(BaseModel):
    name: str
    price: float
    @field_validator("price")
    @classmethod
    def positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("price must be > 0")
        return v

class ItemRead(BaseModel):
    id: int
    name: str
    price: float
    model_config = ConfigDict(from_attributes=True)
```

```python
# settings.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    port: int = 8000                       # env: PORT
    database_url: str = "sqlite:///./<<PRODUCT_SLOT: db>>.db"

settings = Settings()
```

Idioms — v1→v2 footguns (do NOT use v1 APIs):
- `model_config = ConfigDict(...)` class attr — NOT inner `class Config:`.
- ORM: `from_attributes=True` — NOT `orm_mode`.
- `model_validate`/`model_dump` — NOT `parse_obj`/`from_orm`/`.dict()`.
- `@field_validator`+`@classmethod` / `@model_validator` — NOT
  `@validator`/`@root_validator`.
- `from pydantic_settings import BaseSettings` (separate package) — NOT
  `from pydantic import BaseSettings`.
- FastAPI 0.115 is native v2; required settings with no env/default
  fail-fast at startup (desirable).

# Alembic 1.13 (pinned) — or skip for small products

**Decide first:** for a small product (≈1–3 features, no data that must
survive schema change), SKIP Alembic — call
`Base.metadata.create_all(bind=engine)` once in the FastAPI `lifespan`
startup. It is correct, zero-config, and tests get a fresh schema. Adopt
Alembic only when the intent needs versioned migrations against persistent
data.

If Alembic is needed — `migrations/env.py` (single shared Base, same URL
as the app):

```python
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

from <<PRODUCT_SLOT: pkg>>.db.base import Base, DATABASE_URL
import <<PRODUCT_SLOT: pkg>>.models  # noqa: F401  import ALL models so metadata is full

config = context.config
config.set_main_option("sqlalchemy.url", DATABASE_URL)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata  # SINGLE metadata — never per-feature

def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True)  # SQLite ALTER; no-op elsewhere
        with context.begin_transaction():
            context.run_migrations()

def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata, literal_binds=True,
        dialect_opts={"paramstyle": "named"}, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()

run_migrations_offline() if context.is_offline_mode() else run_migrations_online()
```

Idioms:
- `target_metadata = Base.metadata` (the ONE shared Base); import every
  model module in `env.py` first.
- Always `render_as_batch=True` (SQLite ALTER); keep `sqlalchemy.url` empty
  in `alembic.ini` (env.py injects the app's `DATABASE_URL`).
- `alembic init migrations` → wire env.py → `alembic revision
  --autogenerate -m "init"` → `alembic upgrade head`.

# pytest 8 (+ vitest) (pinned)

`<<PRODUCT_SLOT: backend>>/tests/conftest.py` (in-memory SQLite,
per-test isolation via `get_db` override):

```python
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from <<PRODUCT_SLOT: pkg>>.main import app
from <<PRODUCT_SLOT: pkg>>.db.base import Base, get_db


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def client(db_session):
    def _override():
        yield db_session
    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
```

`pyproject.toml` discovery (scoped to product; never recurse otto dirs):

```toml
[tool.pytest.ini_options]
minversion = "8.0"
testpaths = ["<<PRODUCT_SLOT: backend>>/tests"]
python_files = ["test_*.py"]
addopts = "-q --strict-markers --strict-config"
norecursedirs = ["otto_logs", ".otto", ".venv", "node_modules", ".git"]
```

Idioms:
- pytest 8: a test that `return`s a value **fails** — assert, never
  return; `yield` in a test fn is an error.
- `StaticPool` + `check_same_thread=False` mandatory (`TestClient`
  threadpool would otherwise see an empty per-thread in-memory DB).
- Override `get_db`, not the engine; always
  `app.dependency_overrides.clear()` (in the `client` fixture).
- vitest 2.x: `vite.config.ts` → `test: { globals: true, environment:
  'jsdom', setupFiles: './src/setupTests.ts' }`; `setupTests.ts` imports
  `@testing-library/jest-dom`, `afterEach(cleanup)`.

# Ports, start.sh & manifest assembly (pinned)

otto injects `PORT` (backend), `VITE_PORT`/`OTTO_BROWSER_PORT` (frontend),
`OTTO_BROWSER_BASE_URL`. The product MUST honor injected ports and never
hard-code or `pkill`.

`start.sh` (verbatim; fill `<<PRODUCT_SLOT>>`):

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_PORT="${PORT:-8000}"
FRONTEND_PORT="${VITE_PORT:-${OTTO_BROWSER_PORT:-5173}}"
export VITE_API_URL="${VITE_API_URL:-http://127.0.0.1:${BACKEND_PORT}}"

export npm_config_cache=/tmp/otto-npm-cache
cd "$ROOT/<<PRODUCT_SLOT: frontend_dir>>" && npm ci --no-audit --fund=false
cd "$ROOT/<<PRODUCT_SLOT: backend_dir>>"
if command -v uv >/dev/null 2>&1; then uv sync --frozen; PYRUN=(uv run)
else python3.12 -m venv "$ROOT/.venv"; "$ROOT/.venv/bin/pip" install --quiet -e .; PYRUN=("$ROOT/.venv/bin/python" -m); fi

PIDS=()
shutdown() { trap - TERM INT EXIT; for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done; wait 2>/dev/null || true; }
trap shutdown TERM INT EXIT

"${PYRUN[@]}" uvicorn <<PRODUCT_SLOT: asgi_module>>:app --host 127.0.0.1 --port "$BACKEND_PORT" &
PIDS+=($!)
( cd "$ROOT/<<PRODUCT_SLOT: frontend_dir>>" && npm run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT" --strictPort ) &
PIDS+=($!)

wait_up() { local url="$1" i; for i in $(seq 1 60); do curl -fsS -o /dev/null "$url" && return 0; sleep 1; done; echo "timeout: $url" >&2; return 1; }
wait_up "http://127.0.0.1:${BACKEND_PORT}/api/health"
wait_up "http://127.0.0.1:${FRONTEND_PORT}/"
echo "READY backend=${BACKEND_PORT} frontend=${FRONTEND_PORT}"
wait -n "${PIDS[@]}"
```

Idioms (invariant):
- Never hard-code 8000/5173/3000 in source — backend reads
  `os.environ["PORT"]`, frontend reads `process.env.VITE_PORT` (config) /
  `import.meta.env.VITE_API_URL` (client).
- Exactly one API-base source of truth: dev proxy (`/api` in
  `vite.config.ts`) XOR `import.meta.env.VITE_API_URL`. Not both. The proxy
  makes requests same-origin (no CORS needed).
- `--strictPort` on Vite: fail loudly on a busy port, never drift to +1.
- Bounded readiness loop (≤60 tries), never `while true`. Foreground
  servers, exit on TERM/INT; `trap` reaps only this script's PIDs.
- **No `pkill`/`killall`/`fuser -k`/`lsof|kill`** — broad cleanup kills
  otto's concurrent verifies (fix-12 collision class).
- `npm ci` with `npm_config_cache=/tmp/otto-npm-cache`; prefer `uv sync
  --frozen`, fall back to venv+pip.
