# SmartFire Video TestKit

## Test roles

**Fake Video Provider**:
A deterministic implementation of the SmartFire Video Provider Contract used to develop and verify a Provider consumer without WVP, a GB28181 Gateway or physical video equipment.
_Avoid_: WVP mock, SIP device, business backend

**GB28181 Device Simulator**:
A controllable protocol peer that behaves like an IPC or NVR toward a Provider and can exercise registration, catalog, live media and device-record scenarios.
_Avoid_: Fake Video Provider, real camera, SmartFire device asset

**Provider Under Test**:
The Provider implementation currently receiving black-box contract and protocol scenarios, such as the Fake Video Provider, WVP Provider or sipgo Gateway.
_Avoid_: simulator, SmartFire consumer

**Contract Runner**:
The black-box test actor that invokes a Provider through the shared HTTP contract and evaluates only externally observable behavior.
_Avoid_: Provider implementation test, simulator control plane

## Interfaces and identities

**Provider Interface**:
The versioned HTTP behavior exposed under `/provider/v1` and shared by the Fake Video Provider, WVP Provider and sipgo Gateway.
_Avoid_: WVP original API, TestKit control API

**TestKit Control Interface**:
The HTTP behavior exposed under `/testkit/v1` for resetting deterministic scenarios, controlling simulated devices and observing test state.
_Avoid_: Provider Interface, production management API

**Protocol Source Identity**:
The stable GB Device ID and GB Channel ID pair used to identify a video source across Provider implementations.
_Avoid_: Provider database ID, Provider Stream Key

**Provider Stream Key**:
A short-lived opaque identity created by a Provider for a live or playback source stream.
_Avoid_: business device ID, GB Channel ID

## Evidence

**Simulator Conformance**:
Evidence that an implementation satisfies the shared contract and deterministic simulator scenarios.
_Avoid_: vendor compatibility, standard certification

**Vendor Compatibility**:
Evidence produced by testing with identified physical equipment, firmware, codec and network settings.
_Avoid_: simulator conformance
