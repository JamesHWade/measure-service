# Follow an extracted-ion chromatogram back to its scans

At 0.5 seconds the example trace reads 5 counts. Two centroids contribute:
2 counts at m/z 100 and 3 counts at m/z 101. The native bundle retains their
source row numbers, 11 and 12. The report lets you follow that calculation
from the plotted point to the original scan.

## Extract the example

From the service checkout, run the installed client against the
[synthetic native acquisition](../tests/fixtures/native_acquisition_xic_v1/manifest.dcf).
Use a store you can reopen and a new directory for the report. The R installation
must provide `measure::read_measure_acquisition()` and `measure::measure_ms_trace()`.

```sh
measure-service-client xic tests/fixtures/native_acquisition_xic_v1 \
  --stream ms-xic --polarity positive --ms-level 1 --mz-min 100 --mz-max 101 \
  --store ./ms-store --output ./ms-report
```

The client starts the local stdio service and prints the retained trace ID.
To select a particular R installation, supply both `--r` and `--r-library`
with absolute paths. To use your own data, supply a directory written by
`measure::write_measure_acquisition()`. The inclusive m/z window uses the unit
declared by that stream.

## Read the five scans

Open `ms-report/report.html`. Select the first marker, then follow the links
in **Contributing rows**. The scan index orders the extraction. The centroid
index is one-based within its scan. A source row number is available when the
input recorded it.

| Time (s) | Source scan | Signal (counts) | What the acquisition records |
| ---: | --- | ---: | --- |
| 0.5 | raw-001 | 5 | Two selected centroids. One has unknown saturation status. |
| 1.0 | raw-002 | — | Missing scan. |
| 1.5 | raw-003 | 0 | Acquired scan with no centroids. |
| 2.0 | raw-004 | — | A selected centroid was saturated. Both selected rows are available for inspection. |
| 2.5 | raw-005 | 7 | One centroid in the window. |

The plot leaves a gap for each unavailable value. The CSV files contain all scan
results and selected centroids, including rows associated with saturation. The JSON
document also records the input digest, selectors, units, worker fingerprint and
R backend versions. These are declared detector counts. Compound identification
would require additional evidence.

## Reopen a retained trace

Use the ID printed by the first command and the same store. This reads the
stored evidence in a new service process and checks its SHA-256 digest before
writing the files. It works without an R executable.

```sh
measure-service-client xic-report "$TRACE_ID" --store ./ms-store --output ./ms-reopened
```

Registering identical native bytes and repeating the same selectors under the
same owner returns the original result. A changed bundle or selection produces
a separate record. The HTML, SVG, JSON and both CSV files can be opened offline.
