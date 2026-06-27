RMBT Test CLI Clients
=====================

This project is a fork of [rtr-nettest/open-rmbt-client-cli](https://github.com/rtr-nettest/open-rmbt-client-cli),
extended to include additional client implementations.

This project contains CLI clients for conducting measurements based on
the RMBT protocol implemented in

- Rust,
- C,
- Java and
- Python (stdlib only, no third-party dependencies).

This implementation is based on the 2012 Java client, originally developed
for a Java Applet (browser plugin) and for the Android client. In 2026 this
code was reimplemented in Rust, C and modern Java (Version 2.x). Please note
that this code is intented for development and verification purposes. 

It is not offered as part of RTR-Nettest.

The Rust, C, and Java clients were developed by RTR-Netztest. The Python
client was added in this fork.


Python client — performance note
---------------------------------

Python's GIL (Global Interpreter Lock) limits true parallel execution across threads. On a 100 Gbit/s back-to-back test system the Python client achieves roughly **8 Gbit/s downstream and 6.3 Gbit/s upstream** (with some run-to-run variation), compared to ~32 Gbit/s in both directions for the Rust client.

For typical home and office connections (up to ~1 Gbit/s) this is not a concern. On high-bandwidth links or low-powered hardware (e.g. a Raspberry Pi or a home router) the CPU may become the bottleneck before the network link is saturated, leading to results that understate the true available bandwidth.


License
-------

This source code is licensed under the Apache License 2.0, found in the
[LICENSE](LICENSE) file in this repository.
The documentation to the project is licensed under the [CC BY-AT 3.0](https://creativecommons.org/licenses/by/3.0/at/deed.de_AT)
license.
