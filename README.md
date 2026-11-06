# OECT control: Keithley 2634B with optional DMM6500 reference

This standalone program runs one OECT with a Keithley **2634B two-channel
SourceMeter** and, when selected, a separate DMM input for the reference
electrode. It preserves the six-column raw-data format. The
2634B runs its native **TSP** command language: `smua` biases and measures the
drain, and `smub` biases and measures the gate.

The optional reference meter is the **Keithley DMM6500** from the earlier
project, at `192.168.1.160`. Feedback and monitor modes use it; no-reference
mode runs with only the 2634B, even if the DMM is disconnected.

## Connections

| Instrument terminal | OECT connection |
| --- | --- |
| 2634B channel A (`smua`) HI | Drain |
| 2634B channel B (`smub`) HI | Current-carrying gate electrode |
| 2634B channels A and B LO | Common OECT source node |
| DMM6500 front INPUT HI (feedback/monitor only) | Separate reference electrode |
| DMM6500 front INPUT LO (feedback/monitor only) | Common OECT source node |

When used, the DMM measures `Vref = V(reference) − V(source)`. The feedback
target uses the same sign: `Eref = Vref`. Keep the voltage-sensing reference
separate from the gate electrode carrying current. The program uses the
2634B's local sensing mode; follow the 2634B manual if you choose to change to
remote sense wiring.
Do not assume that the shared LO node is chassis ground; the 2634B manual
describes floating connections and their limits.

For feedback or monitor mode, the DMM6500 must be physically set to **FRONT**
terminals. Its driver checks SCPI language and front terminals, then selects DC
voltage on a fixed 10 V range with **AUTO input impedance**. On a DMM6500,
AUTO gives more than 10 GΩ input impedance on this range; a 10 MΩ setting can
load a reference electrode.
It disables relative, math, averaging, and limit functions, uses a one-reading
trigger model, and verifies the configuration before the SMU output is enabled
and while reading. These details are specific to the DMM6500.

## LAN connection

The default VISA resource names are:

```text
2634B:  TCPIP0::192.168.1.150::inst0::INSTR
DMM6500: TCPIP0::192.168.1.160::inst0::INSTR
```

Both are VISA **TCPIP INSTR** resources (`inst0`, VXI-11), not raw TCP socket
resources. There is no port number to enter in the GUI. The application uses
PyVISA with the `pyvisa-py` backend; both instruments must be reachable from
the computer on the same network when a DMM mode is selected. **Test both
connections** queries identities in feedback/monitor mode; **Test 2634B
connection** skips the DMM in no-reference mode. Neither test turns on the
2634B outputs.

## Install and run

### Windows

Copy this project folder to a writable location, such as your Documents folder.
Install 64-bit Python 3.13, then open PowerShell in this folder. To install the
packages into that Python installation without a project virtual environment,
run:

```powershell
py -3.13 -m pip install --upgrade pip
py -3.13 -m pip install -r requirements.txt
py -3.13 -m unittest discover -v
py -3.13 gui.py
```

Use `py -3.13 gui.py` to start the GUI later. Keep `py -3.13` in the install and
run commands so they use the same interpreter. `py -3.13 -m pip show PyVISA`
shows the installed package location. A Python installation shared with other
programs also shares its packages, so changing their versions can affect those
programs. If pip says it is "Defaulting to user installation," the packages go
to your Windows user package directory rather than Python's `Lib\site-packages`;
the same `py -3.13` commands can still use them.

Alternatively, to keep this project's packages separate, create a virtual
environment in the project folder:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe gui.py
```

Create the `.venv` on the Windows PC rather than copying a virtual environment
from another computer. If you choose this option, use the last command to start
the GUI later. The `.command` launcher below is for macOS.

### macOS

From this folder:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python gui.py
```

On macOS, `launch_gui.command` starts the same GUI after dependencies are
installed. The GUI opens in **Simulation** mode and makes no LAN connection
until you deselect it. Set the instrument IP addresses in the Connection card
if needed, then use the connection test for your selected reference mode before
a live run.

The GUI selects a **reference mode** and a **sweep pattern** independently. It
shows drain current and gate command in all modes. It also shows measured Vref
when the DMM is used; only feedback mode overlays an Eref target. The raw-data
filename, drain bias, sweep levels and timing, gate bounds, and the applicable
feedback controls remain on the left. **Stop safely** requests zero source
settings and turns both SMU outputs off. Closing the window during a run also
asks to stop. The output file is replaced only after the GUI's overwrite
confirmation.

The default target staircase is 0 to −0.8 V in −0.1 V steps. Default drain
bias is −0.01 V, and both channel current compliance settings are 3 mA. The
program deliberately restricts its voltage controls to ±3 V, even though the
2634B has wider hardware ranges. Check that these settings suit the device
before enabling the outputs.

## Reference modes

| GUI mode | What the sweep levels mean | DMM and Vref | Gate control |
| --- | --- | --- | --- |
| **Reference feedback** (default) | Target Eref | DMM reads Vref; plot shows Vref and target Eref | Vg changes after DMM readings to reduce Eref error |
| **Reference monitor** | Programmed Vg | DMM reads and plots Vref | Vg changes only when the programmed sweep advances |
| **No reference** | Programmed Vg | No DMM connection or Vref plot; raw Vref cells are blank | Vg changes only when the programmed sweep advances |

In monitor and no-reference modes, the first Vg level is programmed before the
SMU outputs turn on. All Vg sweep levels must lie within the selected gate
bounds. Feedback mode instead starts at the separate **Initial gate** setting;
its sweep levels are Eref targets. The feedback controls apply only in feedback
mode.

In feedback mode, after each reference reading the software computes:

```text
error = target Eref − measured Eref
gate change = direction × correction factor × error
```

It limits that change to the selected maximum step and keeps the command
inside the gate bounds. The default direction means decreasing the gate
voltage is expected to decrease Eref; select the opposite direction only if a
small observed gate change in your cell shows that response. The DMM reading
and target are never sign-inverted.

## Sweep patterns and timing

- **Staircase • keep at final** (default): advance from the initial to the final
  level in the selected steps. Hold each level for **Period**, then hold the
  final level for the additional **Keep at final** time.
- **Back and forth • cycles**: sweep initial → final → initial for each cycle,
  repeated **Cycles** times. The turning points are held once, not duplicated
  as adjacent stages. There is no additional final keep in this pattern. For
  example, two cycles over `0, −0.1, −0.2` produce
  `0, −0.1, −0.2, −0.1, 0, −0.1, −0.2, −0.1, 0`.

The **Delay** is inside each level's Period. Rows recorded during that delay
remain in the raw file. The GUI enables **Keep at final** only for staircase
and **Cycles** only for back-and-forth. Both patterns work with every reference
mode: the levels represent Eref targets in feedback and Vg setpoints otherwise.

The read interval is a **host-paced** interval. One TSP request obtains the
2634B's drain and gate current readings. When enabled, the DMM Vref reading
follows over LAN. The readings are not hardware simultaneous. They are also not
guaranteed to occur exactly at the requested interval; network latency,
measurement time, or a busy computer can make a row late. The plotted and
saved drain/gate voltages are verified source commands, not independent
voltage measurements.

The raw output is a tab-delimited `.txt` file with exactly these columns:

```text
Time  GateI  GateV  DrainI  DrainV  Vref
```

`Time` is elapsed seconds, currents are amperes, and voltages are volts. In
no-reference mode, `Vref` is empty rather than a fabricated zero. The default
path is `outputs/oect_reference.txt` inside this project. A measurement or
control error triggers an attempt to set both SMU sources to 0 V and turn both
outputs off. If LAN communication is lost, verify the output indicators at the
2634B itself.

## Offline check

Run the automated tests without instrument connections using the command for
your platform and installation choice above. For example, in a macOS project
virtual environment:

```bash
.venv/bin/python -m unittest discover -v
```

You can also launch the GUI in Simulation mode, choose a new `.txt` output
file, and shorten the period, delay, final keep or cycle count, and read
interval for a quick trial of each mode. Simulation models a reference response
to the gate command when the DMM mode is selected, but cannot identify the real
cell's feedback direction. Before measuring a device, use a short live run on
a suitable dummy load to verify polarity, current limits, read timing, DMM
impedance when applicable, and output shutdown. **The program has only been
tested offline in this workspace; no 2634B or DMM was connected for
validation.**

## Manuals and protocol references

- [Local 2634B manual notes](MANUAL_NOTES.md) list the commands and printed manual pages used for this program.
- [Keithley Series 2600B System SourceMeter Reference Manual, Rev. F](https://download.tek.com/manual/2600BS-901-01F_2600B_Reference_Aug2021.pdf) (includes the 2634B and TSP command reference).
- [Keithley DMM6500 Reference Manual](https://download.tek.com/manual/DMM6500-901-01B_Sept_2019_Ref.pdf).
- [PyVISA resource-name syntax](https://pyvisa.readthedocs.io/en/latest/introduction/names.html) and [PyVISA-py TCPIP INSTR support](https://pyvisa.readthedocs.io/projects/pyvisa-py/en/latest/installation.html).
