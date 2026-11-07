# Fixed chromatogram snapshot reference

`output/` is the byte-identical output of the installed public-MCP/R workflow at
source `9d365268618cda45fa7ffccf9a8bbefc5bfa24a1`. The two ZIPs, six report sets,
CSV index and original workflow receipt retain their execution identities.

`index.html` is a separate presentation generated from the retained receipt.
It does not replace or rewrite the executed reports. `publication.json` lists
the published file identities, `execution.json` identifies the executed build,
and `independent-review.json` describes the data audit and its scope.

The source contains five synthetic traces and four selected inputs. Only two
original successes support an archived-to-fresh comparison. The original QC
and unknown outcomes remain pending even though new computations completed in
the restored store. These records do not claim physical qualification or MoSeS
paper parity.

The raw fixture, executable example, current-backend replay test and numerical
tolerances are described in [the results guide](../../chromatogram-snapshot-results.md).
