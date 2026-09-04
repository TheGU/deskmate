# data-examples

Templates for the files another agent writes into `data/` at runtime. `data/`
is gitignored; this directory is not, so these examples stay in the repository
as the reference shape.

Copy what you need:

```sh
mkdir -p data/brief
cp data-examples/ai-usage.json      data/ai-usage.json
cp data-examples/brief/current.json data/brief/current.json
cp data-examples/brief/morning.md   data/brief/morning.md
cp data-examples/brief/evening.md   data/brief/evening.md
```

Then point the adapters at the files:

```sh
AI_USAGE_SOURCE=file
BRIEF_SOURCE=file
```

Schemas, writing rules and the alert POST body are documented in
[docs/DATA-SOURCES.md](../docs/DATA-SOURCES.md).
