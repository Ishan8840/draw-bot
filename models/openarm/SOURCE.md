# OpenArm model provenance

Derived from https://github.com/enactic/openarm_mujoco at commit
`a3c8a57ec15d385e3c1dbd3fd090ee486b7f258c`, file `v2/openarm_bimanual.xml`.
Copyright 2026 Enactic, Inc. Licensed under Apache-2.0 (included in LICENSE).

The used right-arm visual/collision meshes are copied unchanged. The seven arm
joint frames, axes, limits and inertias are retained. The drawing fixture removes
the left arm, rigidly closes the fingers, adds a fixed marker, mount, board,
lighting and visual settings, sets a 2 ms timestep, and tunes simulation position
servo gains. Upstream actuator force limits are retained. Gravity/Coriolis
compensation is supplied through bounded actuator commands by the drawing controller. This is a simulation fixture,
not a certified hardware controller or a calibrated model of a particular unit.

Regenerate with `python -m scripts.build_openarm_scene --upstream <checkout>`.
The runtime loads only the vendored local model and assets; no upstream clone or
network access is required.
