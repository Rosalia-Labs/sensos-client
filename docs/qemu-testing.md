# QEMU Testing

QEMU testing documentation for this repo lives under:

- [`test/qemu/docs/README.md`](../test/qemu/docs/README.md)

That guide covers the local Apple Silicon and MacPorts-based Debian Trixie ARM64
workflow, including:

- VM artifact layout
- initial install versus disposable `run` boots, including the requirement to
  cleanly shut down the guest at the end of install so changes persist
- guest bootstrap steps
- data-disk testing for `config-storage`
- SSH and extra host port forwards for client/server integration tests
- the distinction between setup API access from the client guest
  (`10.0.2.2:18765` in the standard flow) and the published WireGuard endpoint
  returned by the server (`10.0.2.2:51281` for the first test network)
- the steady-state in-tunnel API target after enrollment
  (`10.254.0.1:8765` in the standard flow)
