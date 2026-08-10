# Domain Documentation

This repository uses a single-context domain documentation layout.

## Before changing behavior

Read:

1. `CONTEXT.md` for canonical terms;
2. `docs/adr/` for accepted and rejected architectural choices;
3. `docs/project/SMARTFIRE-VIDEO-TESTKIT-IMPLEMENTATION-PLAN.md` for planned delivery slices;
4. `docs/project/VERIFICATION-BASELINE.md` for current verified behavior;
5. the referenced SmartFire Video Provider Contract for `/provider/v1` semantics.

## Consumer rules

- Use canonical terms in code, tests, issues and documentation.
- Keep `CONTEXT.md` implementation-free; it is a glossary, not a progress report.
- Record hard-to-reverse and non-obvious trade-offs as ADRs.
- Determine actual completion from source code and fresh verification, not plans or issue status alone.
- Do not call Simulator Conformance “Vendor Compatibility”.
- Provider-specific workarounds belong behind the relevant Adapter or simulator scenario, not in the shared Interface.

## Layout

```text
/
├── CONTEXT.md
├── AGENTS.md
└── docs/
    ├── adr/
    ├── agents/
    └── project/
```
