# RT-BDS
 
a desktop interface for automating room temperature broadband dielectric spectroscopy (BDS) testing across multiple probes. controls a Keysight E4980A LCR meter and a Denkovi 16-channel relay board over USB/serial VISA, logs impedance measurements across frequency sweeps for each probe, computes dielectric constant, and plots everything live with the ability to export to Excel.
 
built for real lab hardware. not a simulation.
 
---
 
## what it does
 
RT-BDS testing means sweeping an LCR meter across a range of frequencies at each of up to 16 physically switched probes, measuring capacitance, dissipation factor, and ESR at each point. with film area and thickness entered, the app also derives the dielectric constant from capacitance in real time. no temperature control is involved, so this is a room-temperature, multi-probe variant: useful for comparing samples, replicates, or positions across a batch without an environmental chamber.
 
doing this manually per probe is tedious and error prone. RT-BDS automates the whole thing: switch to a probe's relay, verify the switch, wait for settling, sweep the LCR across frequency, log/plot/organize the data, repeat for each enabled probe.
 
---
 
## hardware
 
| device | interface | role |
|---|---|---|
| Keysight E4980A LCR Meter | USB (VISA) | sweeps frequency, measures Cp, Df, ESR |
| Denkovi 16-Channel Relay Board | Serial (VISA/ASRL) | switches between up to 16 probes, only one active at a time |
 
communication goes through PyVISA for both devices — the LCR meter as a `USBInstrument`, the relay board as a `SerialInstrument` at 9600 baud, 8 data bits, 1 stop bit, no parity, per the board's manual. the relay board speaks a raw ASCII byte protocol (`off//`, `NN+//` / `NN-//`, `ask//`) and every command is verified by comparing the echoed reply back against what was sent, byte for byte.
 
---
 
## how it works
 
**state machine** — the app runs through a `RUN_STATE` IntFlag enum with combinable states:
 
```python
IDLE | PROBE_SWITCHING | LCR_MEASURING | DONE
RUNNING = PROBE_SWITCHING | LCR_MEASURING  # bitwise combo
```
 
this lets you check `if state & RUN_STATE.RUNNING` instead of comparing against every possible active state individually. there's no temperature-related state since there's no oven in the loop — switching and measuring are the only two things the run loop ever does.
 
**relay switching (break before make)** — before selecting a probe, the software always writes an all-off command first to create a known intermediate state, waits a short break-before-make delay, then writes the target relay pattern. it reads back the board's status afterward and confirms that only the intended relay is enabled, raising an error if verification fails. a minimum command interval is enforced between any two writes to the board so commands can't be sent faster than the hardware can process them. the all-relays-on command is explicitly disabled in software, since energizing every probe at once is never a safe state for RT-BDS. after a verified switch, a configurable settling delay elapses before the LCR sweep starts.
 
**frequency sweep** — the frequency tab lets you define a logspace sweep (first freq, last freq, points per decade) and add manual spot frequencies on top. the combined list gets sorted and deduplicated before being sent to the instrument, with hard limits enforced against the LCR's real range (20 Hz to 300 kHz). each frequency point runs two back to back measurements: Cp+Df in CPD mode and ESR in CSRS mode, with a bus trigger and `*WAI`/`*OPC?` handshake to ensure the instrument is settled before fetching results. overrange readings from the instrument are converted to NaN rather than kept as raw sentinel values.
 
**dielectric constant** — when film area (mm²) and film thickness (µm) are entered for a run, the app derives the dielectric constant from each measured Cp using `k = C·d / (ε₀·A)`, live, per probe, per frequency. leaving both blank skips the calculation; entering only one raises a validation error so a run can't silently produce a meaningless constant.
 
**probe switching** — probes are enabled and assigned a relay index (1–16) on the probe tab; each relay can only be assigned to one probe, and the app rejects duplicate assignments before a run starts. a manual testing panel lets you select any probe's relay directly, read back board status, or force all relays off without starting a full run — disabled automatically while a run is active.
 
**run loop** — a single background thread drives the whole run: connect devices, force all relays off, then for each enabled probe in order — switch relay (verified), wait the settling delay, sweep every frequency in the plan, checking for a stop request before and after each LCR command so nothing hangs waiting on the instrument bus. rows accumulate per probe and get appended to the master dataset as soon as that probe's sweep finishes, so the table and plots update without waiting on the entire run. relays are always forced off in a `finally` block on the way out, whether the run finished, was stopped, or errored.
 
**traceability** — before a run (or an export) starts, the app checks for a Roll ID and Operator name. if either is missing, a modal dialog blocks until both are entered; once confirmed, the values persist for the rest of the session and are written into the exported Metadata sheet.
 
**data acquisition** — measurement rows are appended under a lock as each probe's sweep completes, then the UI table and plots are refreshed on the main thread. there's no rolling instrument-status buffer or background polling loop here, since without an oven there's nothing to poll between measurements — data only changes when a sweep produces it.
 
**Excel export** — on export, results are written to a structured workbook: Metadata, Summary, Probe Index, Frequency Plan, Measurements, and one sheet per probe (all 16 relay slots, whether or not that probe was used in the run). the Probe Index sheet includes hyperlinks to each per-probe sheet. the Summary sheet tallies unique frequencies measured, min/max frequency, and per-column failure counts (Cp, dielectric constant, Df, ESR) alongside a count for each run status seen. all sheets get frozen header rows, autofilters, and autosized columns.
 
**live plots** — three tabbed plot views update during the run: measurements vs frequency (Cp, Df, ESR on a semilog x-axis, one line per enabled probe), a probe comparison view (Cp, Df, ESR at a user-selected frequency, snapped to the nearest frequency actually measured, plotted across probes), and dielectric constant vs frequency per probe with a marker at the selected comparison frequency.
 
**device abstraction** — `devices.py` defines a `Device` base class with `send()` and `close()` interface methods. `KeysightLCR_E4980A` and `DenkoviRelayBoard` each implement the appropriate communication layer. `send()` in `devices.py` handles query vs. command routing for the LCR meter — anything ending in `?` goes through `dev.query()`, everything else through `dev.write()` — while the relay board uses its own raw byte protocol underneath but exposes the same `send()` interface for the manual command console, with the all-on command blocked at that layer too.
 
---
 
## files
 
| file | description |
|---|---|
| `main.py` | full GUI application — all UI, state management, run logic, export | (should separate into different files for better organization and future debugging but it works well now)
| `devices.py` | device classes and VISA communication layer |
 
---
 
## requirements
 
```
pip install pyvisa pyvisa-py pyserial openpyxl matplotlib pandas numpy
```
 
also requires:
- NI-VISA runtime (for USB instrument communication)
- physical hardware or a VISA simulation environment for testing
---
