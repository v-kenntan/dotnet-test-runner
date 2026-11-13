# Coding Standards

Rules for review. Each rule is a judgement call that a linter cannot check.

## Test definitions (`backend/test_definitions/*.yaml`)

- **Config file shapes cite their source.** A step that writes or edits a config file that dotnet reads (`global.json`, `NuGet.config`, `Directory.Build.props`, ...) has a comment with the official doc URL that shows that shape. The written structure matches the doc exactly: same nesting and same keys. A comment that only restates the intent (for example "rename version to workloadVersion") is not a source. Issue #92 shipped a wrong `global.json` layout because of such a comment.
