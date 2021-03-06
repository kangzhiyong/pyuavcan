#
# Copyright (c) 2019 UAVCAN Development Team
# This software is distributed under the terms of the MIT License.
# Author: Pavel Kirienko <pavel.kirienko@zubax.com>
#

from __future__ import annotations
import typing
import pytest
import asyncio
import pyuavcan.transport
import pyuavcan.transport.can.media as _media


class MockMedia(_media.Media):
    def __init__(self, peers: typing.Set[MockMedia], mtu: int, number_of_acceptance_filters: int):
        self._peers = peers
        peers.add(self)

        self._mtu = int(mtu)

        self._rx_handler: _media.Media.ReceivedFramesHandler = lambda _: None  # pragma: no cover
        self._acceptance_filters = [self._make_dead_filter()  # By default drop (almost) all frames
                                    for _ in range(int(number_of_acceptance_filters))]
        self._automatic_retransmission_enabled = False      # This is the default per the media interface spec
        self._closed = False

        self._raise_on_send_once: typing.Optional[Exception] = None

        super(MockMedia, self).__init__()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return asyncio.get_event_loop()

    @property
    def interface_name(self) -> str:
        return f'mock@{id(self._peers):08x}'

    @property
    def mtu(self) -> int:
        return self._mtu

    @property
    def number_of_acceptance_filters(self) -> int:
        return len(self._acceptance_filters)

    def start(self, handler: _media.Media.ReceivedFramesHandler, no_automatic_retransmission: bool) -> None:
        if self._closed:
            raise pyuavcan.transport.ResourceClosedError

        assert callable(handler)
        self._rx_handler = handler
        assert isinstance(no_automatic_retransmission, bool)
        self._automatic_retransmission_enabled = not no_automatic_retransmission

    def configure_acceptance_filters(self, configuration: typing.Sequence[_media.FilterConfiguration]) -> None:
        if self._closed:
            raise pyuavcan.transport.ResourceClosedError

        configuration = list(configuration)                         # Do not mutate the argument
        while len(configuration) < len(self._acceptance_filters):
            configuration.append(self._make_dead_filter())

        assert len(configuration) == len(self._acceptance_filters)
        self._acceptance_filters = configuration

    @property
    def automatic_retransmission_enabled(self) -> bool:
        return self._automatic_retransmission_enabled

    async def send_until(self, frames: typing.Iterable[_media.DataFrame], monotonic_deadline: float) -> int:
        del monotonic_deadline      # Unused
        if self._closed:
            raise pyuavcan.transport.ResourceClosedError

        if self._raise_on_send_once:
            self._raise_on_send_once, ex = None, self._raise_on_send_once
            assert isinstance(ex, Exception)
            raise ex

        frames = list(frames)
        assert len(frames) > 0, 'Interface constraint violation: empty transmission set'
        assert min(map(lambda x: len(x.data), frames)) >= 1, 'CAN frames with empty payload are not valid'
        # The media interface spec says that it is guaranteed that the CAN ID is the same across the set; enforce this.
        assert len(set(map(lambda x: x.identifier, frames))) == 1, 'Interface constraint violation: nonuniform ID'

        timestamp = pyuavcan.transport.Timestamp.now()

        # Broadcast across the virtual bus we're emulating here.
        for p in self._peers:
            if p is not self:
                # Unconditionally clear the loopback flag because for the other side these are
                # regular received frames, not loopback frames.
                p._receive(_media.TimestampedDataFrame(identifier=f.identifier,
                                                       data=f.data,
                                                       format=f.format,
                                                       loopback=False,
                                                       timestamp=timestamp)
                           for f in frames)

        # Simple loopback emulation with acceptance filtering.
        self._receive(_media.TimestampedDataFrame(identifier=f.identifier,
                                                  data=f.data,
                                                  format=f.format,
                                                  loopback=True,
                                                  timestamp=timestamp)
                      for f in frames if f.loopback)

        return len(frames)

    def close(self) -> None:
        if self._closed:
            raise pyuavcan.transport.ResourceClosedError
        else:
            self._closed = True
            self._peers.remove(self)

    def raise_on_send_once(self, ex: Exception) -> None:
        self._raise_on_send_once = ex

    def inject_received(self, frames: typing.Iterable[_media.DataFrame]) -> None:
        timestamp = pyuavcan.transport.Timestamp.now()
        self._receive(_media.TimestampedDataFrame(identifier=f.identifier,
                                                  data=f.data,
                                                  format=f.format,
                                                  loopback=f.loopback,
                                                  timestamp=timestamp)
                      for f in frames)

    def _receive(self, frames: typing.Iterable[_media.TimestampedDataFrame]) -> None:
        frames = list(filter(self._test_acceptance, frames))
        if frames:                                          # Where are the assignment expressions when you need them?
            self._rx_handler(frames)

    def _test_acceptance(self, frame: _media.DataFrame) -> bool:
        return any(map(
            lambda f:
            frame.identifier & f.mask == f.identifier & f.mask and (f.format is None or frame.format == f.format),
            self._acceptance_filters))

    @staticmethod
    def list_available_interface_names() -> typing.Iterable[str]:
        return []  # pragma: no cover

    @staticmethod
    def _make_dead_filter() -> _media.FilterConfiguration:
        fmt = _media.FrameFormat.BASE
        return _media.FilterConfiguration(0, 2 ** int(fmt) - 1, fmt)


@pytest.mark.asyncio    # type: ignore
async def _unittest_can_mock_media() -> None:
    import asyncio
    from pyuavcan.transport.can.media import DataFrame, FrameFormat, FilterConfiguration

    peers: typing.Set[MockMedia] = set()

    me = MockMedia(peers, 64, 3)
    assert len(peers) == 1 and me in peers
    assert me.mtu == 64
    assert me.number_of_acceptance_filters == 3
    assert not me.automatic_retransmission_enabled
    assert str(me) == f"MockMedia(interface_name='mock@{id(peers):08x}', mtu=64)"

    me_collector = FrameCollector()
    me.start(me_collector.give, False)
    assert me.automatic_retransmission_enabled

    # Will drop the loopback because of the acceptance filters
    await me.send_until([
        DataFrame(123, bytearray(b'abc'), FrameFormat.EXTENDED, loopback=False),
        DataFrame(123, bytearray(b'def'), FrameFormat.EXTENDED, loopback=True),
    ], asyncio.get_event_loop().time() + 1.0)
    assert me_collector.empty

    me.configure_acceptance_filters([FilterConfiguration.new_promiscuous()])
    # Now the loopback will be accepted because we have reconfigured the filters
    await me.send_until([
        DataFrame(123, bytearray(b'abc'), FrameFormat.EXTENDED, loopback=False),
        DataFrame(123, bytearray(b'def'), FrameFormat.EXTENDED, loopback=True),
    ], asyncio.get_event_loop().time() + 1.0)
    assert me_collector.pop().is_same_manifestation(
        DataFrame(123, bytearray(b'def'), FrameFormat.EXTENDED, loopback=True))
    assert me_collector.empty

    pe = MockMedia(peers, 8, 1)
    assert peers == {me, pe}

    pe_collector = FrameCollector()
    pe.start(pe_collector.give, False)

    me.raise_on_send_once(RuntimeError('Hello world!'))
    with pytest.raises(RuntimeError, match='Hello world!'):
        await me.send_until([], asyncio.get_event_loop().time() + 1.0)

    await me.send_until([
        DataFrame(123, bytearray(b'abc'), FrameFormat.EXTENDED, loopback=False),
        DataFrame(123, bytearray(b'def'), FrameFormat.EXTENDED, loopback=True),
    ], asyncio.get_event_loop().time() + 1.0)
    assert pe_collector.empty

    pe.configure_acceptance_filters([FilterConfiguration(123, 127, None)])
    await me.send_until([
        DataFrame(123, bytearray(b'abc'), FrameFormat.EXTENDED, loopback=False),
        DataFrame(123, bytearray(b'def'), FrameFormat.EXTENDED, loopback=True),
    ], asyncio.get_event_loop().time() + 1.0)
    await me.send_until([
        DataFrame(456, bytearray(b'ghi'), FrameFormat.EXTENDED, loopback=False),    # Dropped by the filters
    ], asyncio.get_event_loop().time() + 1.0)
    assert pe_collector.pop().is_same_manifestation(
        DataFrame(123, bytearray(b'abc'), FrameFormat.EXTENDED, loopback=False))
    assert pe_collector.pop().is_same_manifestation(
        DataFrame(123, bytearray(b'def'), FrameFormat.EXTENDED, loopback=False))
    assert pe_collector.empty

    me.close()
    assert peers == {pe}
    with pytest.raises(pyuavcan.transport.ResourceClosedError):
        await me.send_until([], asyncio.get_event_loop().time() + 1.0)
    with pytest.raises(pyuavcan.transport.ResourceClosedError):
        me.configure_acceptance_filters([])
    with pytest.raises(pyuavcan.transport.ResourceClosedError):
        me.close()


class FrameCollector:
    def __init__(self) -> None:
        self._collected: typing.List[_media.TimestampedDataFrame] = []

    def give(self, frames: typing.Iterable[_media.TimestampedDataFrame]) -> None:
        frames = list(frames)
        assert all(map(lambda x: isinstance(x, _media.TimestampedDataFrame), frames))
        self._collected += frames

    def pop(self) -> _media.TimestampedDataFrame:
        head, self._collected = self._collected[0], self._collected[1:]
        return head

    @property
    def empty(self) -> bool:
        return len(self._collected) == 0

    def __repr__(self) -> str:  # pragma: no cover
        return f'{type(self).__name__}({str(self._collected)})'
