"""Prove the generated control-plane stubs are consumable from api/.

If `make proto-gen` is broken or the stubs are stale/missing, importing them
here fails and this test stops the build.
"""

from mcsd.controlplane.v1 import control_plane_pb2, control_plane_pb2_grpc


def test_message_stub_round_trips() -> None:
    msg = control_plane_pb2.WorkerMessage(correlation_id="abc")
    assert msg.correlation_id == "abc"


def test_grpc_service_stub_is_present() -> None:
    assert hasattr(control_plane_pb2_grpc, "WorkerServiceStub")
    assert hasattr(control_plane_pb2_grpc, "WorkerServiceServicer")
