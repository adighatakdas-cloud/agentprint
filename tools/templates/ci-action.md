```yaml
on: [push, pull_request]
permissions: { contents: read, security-events: write }  # security-events: for upload-sarif
jobs:
  agentprint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: adighatakdas-cloud/agentprint@v{{version}}  # better: pin the release's commit SHA
        with: { command: pack, upload-sarif: true }  # command: pack or scan
```
