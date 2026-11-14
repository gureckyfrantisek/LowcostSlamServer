# Sample timing

How the server puts a time on every IMU sample, and how to get GPS time from it afterwards. The numbers come
from the bench tests T1 to T5 (`scripts/bench/`, results in `scripts/bench/FIFO_TIMING_HANDOFF.md`).

## What the server does

1. The LIS3DH collects its samples in a 32 sample FIFO and raises **INT1** when it holds `threshold + 1` samples
   (the *watermark*, `IMU_FIFO_THRESHOLD`, default 8 so every 9 samples).
2. The kernel time-stamps that edge in its interrupt handler (`CLOCK_MONOTONIC`). One read per edge takes the
   samples out of the FIFO. A late read costs no samples: the FIFO holds 58 ms at 400 Hz, the worst stall seen in
   the bench was 16.5 ms.
3. The samples between two edges get their time by **linear interpolation** between the edges. The sensor clock
   is 0.5 % off nominal and wanders by about 200 ppm per minute, so counting samples at the nominal rate would be
   5 ms off after 1 s; interpolating between edges follows it.
4. While a recording runs, a **PM QoS request** (10 us) keeps the CPUs out of the deep C-states. Their wake-up
   delays the interrupt time stamps by up to 1 ms. Without the request the p99 error is several hundred us, with
   it 16 us. It is held only while recording.
5. The **PPS** edges of the GNSS receiver are time-stamped by the same kernel clock and written to `pps.csv`.

If the GPIO edges are not available (no libgpiod, no permission, wrong wiring) the reader falls back to reading the
FIFO on a timer. The samples are then timed to about one sample period, and `imu_info.json` says
`"timing": "polling"` and why.

## Files of a recording

| file | content |
|---|---|
| `imu.csv` | `ts,mono_ns,aX,aY,aZ`: `ts` is the wall clock, `mono_ns` the `CLOCK_MONOTONIC` time of the sample, a, the raw 12 bit counts |
| `pps.csv` | `mono_ns,seqno`: the PPS edges on the same clock |
| `gnss.ubx` | the raw UBX stream (RXM-RAWX, RXM-SFRBX, TIM-TM2, TIM-TP); RINEX-convertible with RTKLIB |
| `gnss_index.csv` | `timestamp_ns,mono_ns,offset,length,ubx_class,ubx_id` of every frame in `gnss.ubx`: `mono_ns` is when the read that completed the frame returned |
| `imu_info.json` | settings (rate, range, mg per count), how the samples were timed, and the counters of the recording |
| `meta.json` | start offsets against the camera, the camera files |

mg per count: 1 at +-2 g, 2 at +-4 g, 4 at +-8 g, 12 at +-16 g. The data is raw, with no calibration.

The counters in `imu_info.json` should all be 0 except `samples`, `edges` and `pps`: `lost_edges` (kernel dropped
interrupt events), `lost_samples` (samples the sensor made that were never read), `overruns` (FIFO full), `gaps`
(places where the timing restarted after one of those), `rescued` (the timer had to read because no edge came),
`errors` (failed bus transfers). A gap shows up in `imu.csv` as a jump of `mono_ns` larger than the sample period.

## From `mono_ns` to GPS time

1. Label every PPS edge with its GPS second: the RXM-RAWX frame that arrives within 0.95 s after the edge carries
   `week` and `rcvTow`, the edge is at `week * 604800 + round(rcvTow)` (the receiver puts the PPS on the GPS time
   grid). Use the `mono_ns` of `gnss_index.csv` for the arrival. The wall clock is not needed, so this works
   without a network.
2. The GPS time of a sample is the linear interpolation of its `mono_ns` between the two PPS edges around it.

`scripts/bench/bench_analyze.py` does the same for the bench runs and checks the result against the time marks
the receiver makes itself (TIM-TM2 of the INT1 edges, the receiver clock does not depend on Linux).

## Measured (bench, ROCK Pi X, PM QoS on)

| | |
|---|---|
| PPS edge time stamp, p99 | 36 us |
| kernel edge vs the receiver's time marks, p99 | 16 us |
| interpolated sample time, edge predicted from its two neighbours, p99 | 16 us (max 186 us) |
| FIFO fill when read, median | 9 of 32 samples; watermark edge to read 0.6 ms median, 0.8 ms p99 |
| accelerometer delay (switch closure to acceleration onset, T5) | about +4.6 ms median, robust std 1.5 ms |

The T5 delay is a rough figure: the early taps measured 1.5 ms and the later ones 5 to 7 ms, most likely because
the taps were struck differently. Repeat it with the switch fixed to the sensor body before relying on it.

On the real server run (2 minutes, 100 Hz): 12 127 samples, 0 lost, 0 gaps, 0 errors, PPS interval p99 deviation
43 us, 121 of 121 RAWX frames labellable.

## Data volume

Measured on that run: `imu.csv` 44 bytes per sample, `gnss.ubx` plus `gnss_index.csv` 1.6 KB per second at 1 Hz.

| IMU rate | per hour |
|---|---|
| 100 Hz | 16 MB |
| 200 Hz | 32 MB |
| 400 Hz | 63 MB |
| GNSS 1 Hz | 6 MB |

The video stays on the camera card, it is not part of these numbers.
