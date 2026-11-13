# Install a local alpha build

Build a directory bundle for one exact service commit, Python interpreter and
platform. The bundle contains the `measure-service` wheel, every resolved
Python dependency wheel, the reviewed Python lock, SHA-256 hashes, and the
selected `measure` and `measure.sec` Git commit IDs. Python installation runs
from this local wheelhouse without a package index. It does not install R
packages.

This is an alpha installation path, not a signed release. The hashes detect
changed or missing bundle files relative to `manifest.json`; they do not prove
who created a bundle. The R verifier checks the selected library, the loaded
package paths and each installed package's `RemoteSha` metadata. It does not
compare installed R package files byte-for-byte with the Git source archive.

## Prepare a Python build environment

Use the same Python patch version and platform for dependency resolution,
bundle construction and installation. The separate builder environment needs
pip and these exact build tools:

```sh
python3.12 -m venv /tmp/measure-alpha-builder
/tmp/measure-alpha-builder/bin/python -m ensurepip --upgrade
/tmp/measure-alpha-builder/bin/python -m pip install \
  pip==25.3 build==1.3.0 setuptools==80.9.0 wheel==0.45.1
```

The builder records Python, pip, build, setuptools and wheel versions in the
manifest. Build from a clean service Git checkout. The builder captures its
full `HEAD` commit and constructs the wheel from `git archive` for that exact
commit, not from live or ignored checkout files. It refuses a dirty checkout
and an output directory that already exists.

## Resolve and review Python dependencies

Create a clean environment for the target Python and platform, install the
selected service source once to resolve its runtime dependencies, then save
the resolved environment without the service distribution. Review the lock
before building; it must contain one exact `name==version` line for each
runtime dependency, including transitive dependencies. Do not include the
service itself, editable installs, URLs, markers or version ranges. SHA-256
hashes may follow each pin; when any pin has hashes, every pin must have at
least one.

```sh
python3.12 -m venv /tmp/measure-alpha-resolve
/tmp/measure-alpha-resolve/bin/python -m ensurepip --upgrade
/tmp/measure-alpha-resolve/bin/python -m pip install /path/to/clean/measure-service
/tmp/measure-alpha-resolve/bin/python - <<'PY' > /tmp/measure-alpha-python.lock
import subprocess
import sys

lines = subprocess.check_output(
    [sys.executable, "-m", "pip", "freeze"], text=True
).splitlines()
for line in lines:
    if not line.lower().startswith("measure-service"):
        print(line)
PY
```

Use an empty runtime-only environment so the lock does not accidentally include
test or build tools. The builder resolves the lock for its own Python and
platform, requires wheels for every pin and rejects any additional transitive
package that was not pinned. The resulting lock is platform-specific. Build a
separate bundle for another Python patch, operating system or architecture.
Wheel compatibility is checked by pip; native system libraries and operating
system packages are outside the bundle.

## Build the bundle

Choose and review the exact R backend commits first. The following captures the
full commits from two local checkouts; make sure those checkouts are on the
revisions you intend to install. The builder rejects branch names and short
IDs and never reads R package metadata to guess a commit.

```sh
MEASURE_COMMIT="$(git -C /path/to/measure rev-parse --verify 'HEAD^{commit}')"
MEASURE_SEC_COMMIT="$(git -C /path/to/measure.sec rev-parse --verify 'HEAD^{commit}')"

/tmp/measure-alpha-builder/bin/python scripts/build_alpha_bundle.py build \
  --source /path/to/clean/measure-service \
  --requirements /tmp/measure-alpha-python.lock \
  --measure-commit "$MEASURE_COMMIT" \
  --measure-sec-commit "$MEASURE_SEC_COMMIT" \
  --output /tmp/measure-alpha-bundle
```

By default, pip downloads dependency wheels while building. To stage without
network access, provide a local wheel directory containing a wheel for every
pin:

```sh
/tmp/measure-alpha-builder/bin/python scripts/build_alpha_bundle.py build \
  --source /path/to/clean/measure-service \
  --requirements /tmp/measure-alpha-python.lock \
  --measure-commit "$MEASURE_COMMIT" \
  --measure-sec-commit "$MEASURE_SEC_COMMIT" \
  --wheelhouse /path/to/reviewed-python-wheels \
  --output /tmp/measure-alpha-bundle
```

The generated manifest records the service source commit, service wheel
version and SHA-256, all dependency wheel names, versions and hashes, the
resolved Python lock hash, and the two explicit R commits. It does not move a
branch or tag. Keep the bundle directory intact; copying a wheel or changing a
lock file invalidates its manifest checks. Generated install requirements
contain the SHA-256 of every staged wheel, including the service wheel. Rebuild
older bundles created before these install hashes were added.

## Install Python wheels offline

Use a fresh environment with the exact Python version and platform recorded in
the manifest. The installer verifies all bundle hashes before it runs pip,
uses `--require-hashes` and only the staged wheelhouse, ignores inherited
`PIP_*` redirects and configuration, checks dependency consistency, then
checks installed distribution versions and verifies that the installed
`measure_service/` and `measure_methods/` files match the service wheel.

```sh
python3.12 -m venv /tmp/measure-alpha-installed
/tmp/measure-alpha-installed/bin/python -m ensurepip --upgrade
/tmp/measure-alpha-installed/bin/python scripts/build_alpha_bundle.py install \
  --bundle /tmp/measure-alpha-bundle
```

The installer force-reinstalls the listed Python packages, so use a dedicated
environment. It never invokes R or installs R source. Its package-file check
does not hash generated entry-point scripts or `.dist-info` metadata. The
service wheel itself is checked by SHA-256 before installation.

## Install and verify R backends

Install R 4.5.0 or newer. Create an explicit package library and set both
`R_LIBS` and `R_LIBS_USER` to it before installation and when running the
service. The two library variables put this selected library first so an
inherited library cannot shadow the pinned packages.

```sh
ALPHA_BUNDLE=/tmp/measure-alpha-bundle
R_LIBRARY=/tmp/measure-alpha-R-library
mkdir -p "$R_LIBRARY"
export R_LIBS="$R_LIBRARY"
export R_LIBS_USER="$R_LIBRARY"
export MEASURE_COMMIT="$(/tmp/measure-alpha-installed/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["r_backends"]["measure"]["commit"])' "$ALPHA_BUNDLE/manifest.json")"
export MEASURE_SEC_COMMIT="$(/tmp/measure-alpha-installed/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["r_backends"]["measure.sec"]["commit"])' "$ALPHA_BUNDLE/manifest.json")"

Rscript -e 'if (getRversion() < "4.5.0") stop("R 4.5.0 or newer is required"); options(repos=c(CRAN="https://cloud.r-project.org")); install.packages(c("remotes", "jsonlite", "tibble", "recipes"), lib=Sys.getenv("R_LIBS_USER"))'
Rscript -e 'options(repos=c(CRAN="https://cloud.r-project.org")); remotes::install_github(paste0("JamesHWade/measure@", Sys.getenv("MEASURE_COMMIT")), lib=Sys.getenv("R_LIBS_USER"), dependencies=NA, upgrade="never")'
Rscript -e 'options(repos=c(CRAN="https://cloud.r-project.org")); remotes::install_github(paste0("JamesHWade/measure.sec@", Sys.getenv("MEASURE_SEC_COMMIT")), lib=Sys.getenv("R_LIBS_USER"), dependencies=FALSE, upgrade="never")'

/tmp/measure-alpha-installed/bin/python scripts/build_alpha_bundle.py verify \
  --bundle "$ALPHA_BUNDLE" \
  --r-library "$R_LIBRARY"
```

The R commands use immutable commit IDs from the bundle, not moving branch
names. `verify` checks R is at least 4.5.0, the selected library is first in
`.libPaths()`, the default `find.package()` result and loaded namespace path for
each backend are inside that library, and `RemoteSha` equals the selected full
commit. This identifies the installed package metadata and the package files
R resolved from the selected library; it does not prove that those files match
the source commit byte-for-byte. It also checks Python versions and package
files again. The service
inherits `R_LIBS` and `R_LIBS_USER` from its client environment; keep both
pointing at the verified library.

The R source commits are fixed; CRAN dependencies are not locked by this
bundle. The explicit worker packages and any dependencies they need come from
the configured CRAN repository at install time. The `measure.sec` install uses
`dependencies=FALSE` so its `Remotes` metadata cannot replace the selected
`measure` commit. For a byte-for-byte
reproducible R environment, resolve and retain CRAN package sources and their
system dependencies separately. This workflow does not claim that CRAN
dependency state is frozen across platforms or dates.
