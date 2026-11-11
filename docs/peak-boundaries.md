# Peak boundaries and overlap warnings

The chromatogram worker keeps the peak rows returned by the installed
`measure` backend, then projects them to a fixed service record:
`peak_id`, `location`, `height`, `left_base`, `right_base`, and `area`, with
`area_basis` and `prominence` when those fields are present. Extra backend
columns are ignored. A nonempty backend table missing any required field
fails the analysis; an empty peak table is valid.

The worker checks finite recorded integration ranges after peak integration.
If two ranges overlap over an interval with nonzero width, the analysis stores
one backend warning. Ranges that only meet at a shared boundary do not trigger
it. The check reports the bounds returned by the backend; the service does not
change boundaries or recalculate areas. For overlapping ranges, shared signal
may contribute to more than one stored area.

Existing analyses keep their original peak rows, areas, warnings, provenance
and digests. The evidence report can show a separate **Derived view
diagnostic** when the stored finite ranges overlap in their interiors. That
notice appears only in `report.html`: it does not edit the stored result or add
fields to `evidence.json`, trace CSV, peak CSV, or the evidence digest. Older
frozen exports remain valid and reconstruct their original bytes and values.

The R worker source digest is recorded with each new analysis. Since the
worker now validates and projects backend peak rows and records overlap
warnings, new analyses have a worker digest for these updated source bytes.
Historical analyses retain the digest for the worker that produced them.
Installed R package fingerprints continue to identify the package code
separately.

An overlap warning describes recorded interval geometry. It does not identify
compounds, resolve coelution, establish a correct baseline, or qualify an
instrument or analytical method. Detector behavior, acquisition settings,
sample preparation, and physical performance remain outside this warning
check. The service does not claim manuscript parity or physical qualification
from the presence or absence of an overlap warning.
