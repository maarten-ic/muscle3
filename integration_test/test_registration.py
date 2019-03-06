from unittest.mock import patch

import pytest
from ymmsl import Conduit, Operator, Port, Reference

from libmuscle.mmp_client import MMPClient


def test_registration(mmp_server):
    client = MMPClient('localhost:9000')
    instance_name = Reference('test_instance')
    port = Port(Reference('test_in'), Operator.S)

    client.register_instance(instance_name, ['tcp://localhost:10000'],
                             [port])

    servicer = mmp_server._MMPServer__servicer
    registry = servicer._MMPServicer__instance_registry

    assert registry.get_locations(instance_name) == ['tcp://localhost:10000']
    assert registry.get_ports(instance_name)[0].name == 'test_in'
    assert registry.get_ports(instance_name)[0].operator == Operator.S


def test_wiring(mmp_server):
    client = MMPClient('localhost:9000')

    client.register_instance(Reference('macro'), ['direct:macro'], [])

    conduits, peer_dims, peer_locations = client.request_peers(
            Reference('micro[0]'))

    assert Conduit(Reference('macro.out'), Reference('micro.in')) in conduits
    assert Conduit(Reference('micro.out'), Reference('macro.in')) in conduits

    assert peer_dims[Reference('macro')] == []
    assert peer_locations['macro'] == ['direct:macro']

    with patch('libmuscle.mmp_client.PEER_TIMEOUT', 0.1), \
            patch('libmuscle.mmp_client.PEER_INTERVAL_MIN', 0.01), \
            patch('libmuscle.mmp_client.PEER_INTERVAL_MAX', 0.1):
        with pytest.raises(RuntimeError):
            client.request_peers(Reference('macro'))

    for i in range(50):
        instance = Reference('micro[{}]'.format(i))
        location = 'direct:{}'.format(instance)
        client.register_instance(instance, [location], [])

    with patch('libmuscle.mmp_client.PEER_TIMEOUT', 0.1), \
            patch('libmuscle.mmp_client.PEER_INTERVAL_MIN', 0.01), \
            patch('libmuscle.mmp_client.PEER_INTERVAL_MAX', 0.1):
        with pytest.raises(RuntimeError):
            client.request_peers(Reference('macro'))

    for i in range(50, 100):
        instance = Reference('micro[{}]'.format(i))
        location = 'direct:{}'.format(instance)
        client.register_instance(instance, [location], [])

    conduits, peer_dims, peer_locations = client.request_peers(
            Reference('macro'))

    assert Conduit(Reference('macro.out'), Reference('micro.in')) in conduits
    assert Conduit(Reference('micro.out'), Reference('macro.in')) in conduits

    assert peer_dims[Reference('micro')] == [100]
    assert peer_locations['micro[22]'] == ['direct:micro[22]']
