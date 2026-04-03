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
