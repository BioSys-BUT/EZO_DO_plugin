# Pioreactor DO Plugin Pack

Dissolved oxygen (DO) integration for Pioreactor using an Atlas Scientific EZO-DO circuit.

This repository package provides three Python plugins that work together:

- `atlas_ezo_do.py` — shared low-level I2C communication helper for Atlas EZO-DO.
- `do_reading.py` — continuous DO tracking job, MQTT publishing, and DB sink registration.
- `do_calibration.py` — guided calibration protocol in **Calibration → Protocols**.

It also includes UI and export dataset YAML files in subfolders:

- `ui/contrib/jobs/do_reading.yaml`
- `ui/charts/do.yaml`
- `exportable_datasets/do_readings.yaml`

## Requirements

- Pioreactor installed and running
- Atlas Scientific EZO-DO board + compatible DO probe
- I2C wiring configured correctly (EZO-DO must be in I2C mode, not UART)
- Pioreactor documentation familiarity:
  - [Plugin introduction](https://docs.pioreactor.com/developer-guide/intro-plugins)
  - [Hardware calibrations](https://docs.pioreactor.com/user-guide/hardware-calibrations)
  - [Adding calibration types](https://docs.pioreactor.com/developer-guide/adding-calibration-type)

## Installation

1. Copy all plugin files from this repository into:
   - `~/.pioreactor/plugins/`
2. Restart Pioreactor services (or reboot the unit) so plugins are reloaded.
3. Open Pioreactor UI and confirm:
   - `do_reading` is available in job controls.
   - DO chart appears in Overview (if configured in your UI setup).
   - **Atlas EZO-DO (dissolved oxygen)** protocol appears in Calibration → Protocols.

## Pioreactor Configuration (required)

After copying plugins, add the following section in Pioreactor configuration (via UI Config editor or config file):

```ini
[do_reading.config]
i2c_channel_hex=0x61
time_between_readings=2.0
```

and

```ini
[ui.overview.charts]
...
do_readings=1
```

Notes:

- `i2c_channel_hex` must match your EZO-DO board address (default Atlas I2C address is `0x61`).
- `time_between_readings` must be at least `2.0`, otherwise `do_reading` will fail to start.

## Plugin Overview

### 1) `atlas_ezo_do.py`

Shared helper module for EZO-DO I2C operations:

- Create probe connection from Pioreactor config
- Send commands and parse EZO responses
- Handle Atlas status codes (including pending/no-data)
- Verify I2C mode and set board address when needed
- Provide averaged DO reads (mg/L)

### 2) `do_reading.py`

Continuous DO acquisition job:

- Runs a background job `do_reading`
- Publishes DO values to MQTT topic `do_reading/DO`
- Streams values into `do_readings` table for charting/export
- Enforces minimum read interval (`time_between_readings >= 2.0s`)
- Pauses/resumes reads when the unit enters sleeping/ready states

### 3) `do_calibration.py`

Guided Atlas EZO-DO calibration protocol:

- Available in Calibration → Protocols as **Atlas EZO-DO (dissolved oxygen)**
- Clears existing probe calibration (`Cal,clear`)
- Calibrates to air (`Cal`) with configurable expected DO (default 8.26 mg/L)
- Optional zero point (`Cal,0`) using a 0 mg/L calibration solution
- Verifies calibration status (`Cal,?`)
- Stores calibration record in Pioreactor calibration storage for traceability/export

The EZO-DO board stores calibration internally; the Pioreactor record captures which points were used, measured values, and EZO status text.

## Calibration Workflow

1. Stop `do_reading` on the target unit.
2. Open Calibration → Protocols → **Atlas EZO-DO (dissolved oxygen)**.
3. Configure protocol options (include zero point, command timeout, samples per checkpoint, expected air DO).
4. Clear existing calibration on the probe.
5. Expose the probe to air, wait for stable readings, then run **Calibrate to air**.
6. Optionally place the probe in 0 mg/L solution and run **Calibrate to zero**.
7. Finalize to verify status and save the calibration record.

Before calibration, ensure temperature, salinity, and pressure compensation settings on the EZO-DO are correct for your setup (see Atlas Scientific documentation).

## Operational Notes

- Do not run `do_reading` simultaneously with calibration.  
  Stop DO tracking first, then run calibration to avoid I2C contention and transient EZO errors.
- During each calibration step, wait for probe stabilization before pressing Continue (use the DO chart if enabled).
- If the EZO-DO is in UART mode, switch it to I2C first; the plugin cannot change UART mode over I2C.
- If Pioreactor UI shows protocol-loading issues after restart, open the Plugins tab once and retry (known behavior on some Pioreactor software versions).
- DO dataset can be exported via the standard **Export data** module as `.csv`.

## Repository Structure

```text
atlas_ezo_do.py
do_reading.py
do_calibration.py
ui/
  charts/do.yaml
  contrib/jobs/do_reading.yaml
exportable_datasets/
  do_readings.yaml
```

## License

MIT License
