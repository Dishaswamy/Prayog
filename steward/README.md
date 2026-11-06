# Prayog Steward

The **Steward** is Prayog's optional AI-assisted experiment support component. It can analyse eligible failed runs, surface likely causes, and provide guidance through the Prayog platform.

The Steward is not required for core authentication, access approval, GPU scheduling, or experiment tracking. Prayog can run with the Steward disabled.

## For users

You do not install or run the Steward separately. Sign in to the Prayog web application and use the Steward features exposed for your role when the service is enabled.

For the overall platform workflow, start with the [main Prayog README](../README.md). For command-line experiment workflows, see the [CLI guide](../cli/README.md).

## For developers and operators

The Steward is split into a server-side package and an isolated Runtime:

- `prayog_steward`: server-side integration, contracts, sanitisation, sessions and task handling.
- `steward_runtime`: model adapters and the Runtime API. It does not hold platform database credentials.
- `DESIGN.md`: architecture, setup, security boundaries, configuration, evaluation and operational details.
- [API_STEWARD.md](../API_STEWARD.md): HTTP endpoints exposed by the platform.

Read [DESIGN.md](DESIGN.md) before changing or deploying the Steward. Keep model execution isolated from the platform database and follow the documented security boundaries.
