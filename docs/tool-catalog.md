# Tool catalog and schema profiles

The service exposes a versioned catalog of MCP tools. Catalog version `1.0`
assigns each of the 76 registered tools to exactly one stable group. MCP
`Tool._meta` carries that group under the key
`measure_service/tool_group`.

## Tool groups

| Group | Workflow |
| --- | --- |
| `discovery` | Registry, method and numerical capabilities |
| `traces` | Scalar trace import, inspection, analysis and peak quantitation |
| `sec` | Relative SEC analysis and comparison |
| `batches` | Declared analytical batches, execution, recovery and snapshots |
| `evidence` | Exact source, result-table and analysis evidence pages |
| `experiments` | Experiment designs, attempts, results and mapped result tables |
| `methods` | Typed chromatography method declarations |
| `observations` | Analytical contracts, observations, snapshots and Gaussian producers |
| `retention` | Ideal retention models and predictions |
| `campaigns` | Synthetic campaign protocols, proposals and exact consumption |

[The generated reference](tools.md) lists every tool and its group.

Set `MEASURE_MCP_TOOL_GROUPS` to a JSON array of group names to expose only
those groups:

```sh
MEASURE_MCP_TOOL_GROUPS='["discovery", "traces", "evidence"]' measure-service
```

If the variable is unset, all 76 tools are enabled. `[]` enables none. The
value is limited to 1,024 UTF-8 bytes and ten unique known group names. Invalid
JSON, a non-array value, duplicate names, unknown names, or an oversized value
fails configuration validation without echoing the supplied value. Group
selection is checked once at startup and enforced for both discovery and
direct calls. A disabled tool is rejected before identity lookup, tool argument
validation or storage access. The setting is a deployment-wide catalog filter;
it does not change the service's identity,
access, retention, or storage policy.

The trace-only example offers 17 tools. Add `sec` or `batches` for those
workflows; groups do not implicitly enable other groups. The installed
two-account numerical pilot additionally needs `experiments` and `sec` along
with `discovery`, `traces` and `evidence`. Select the full default catalog when
qualifying workflows that span other groups.

## Canonical JSON Schemas

`MEASURE_MCP_TOOL_SCHEMA_PROFILE` defaults to `canonical`. Canonical input
schemas use JSON Schema Draft 2020-12. Method values remain strict at runtime:
booleans and numeric strings are rejected, numbers must be finite, and positive,
nonnegative, and fraction bounds are checked both by the declared schema and
by Pydantic validation. Runtime validators also enforce relational conditions
such as solvent fractions summing to one.

The `scripts/check_tool_schemas.py` check verifies Draft 2020-12 validity and
exact registry coverage for the complete catalog. It rejects the nonstandard
numeric keywords `gt`, `ge`, `lt`, and `le`. It reports canonical `oneOf`,
`prefixItems`, and numeric array maxima above 16,000 as notices for review.

## Optional function-host schema projection

Set `MEASURE_MCP_TOOL_SCHEMA_PROFILE=function-host/1` to request the bounded
projection for hosts that have reported restrictions on these schema forms.
The projection deep-copies each schema before changing it. It currently:

- Changes a `oneOf` union to `anyOf` and removes its discriminator only after
  proving that each `$defs` branch requires the same string `kind` field with a
  different literal value. Such branches are disjoint by construction.
- Rewrites a two-element tuple whose two positions are both numbers to a
  numeric `items` schema while retaining `minItems: 2` and `maxItems: 2`.
- Omits `maxItems` when it exceeds 16,000 on a numeric array. The schema
  description states the original runtime maximum. For example, each trace
  `axis` and `signal` still has a runtime maximum of 50,000 points.

The adapter fails on an unproven `oneOf`, an unsupported `prefixItems` form, or
a large nonnumeric array. The function-host profile covers these reviewed
constructs only. It does not guarantee acceptance by OpenAI strict conversion
or by a particular vendor endpoint; callers must inspect the generated schema
for their target.

Use the checker against either profile with:

```sh
python scripts/check_tool_schemas.py
python scripts/check_tool_schemas.py --profile function-host/1
```

`python scripts/render_reference.py --json` exports the selected advertised
catalog. Reference `--write`/`--check` and the schema checker always inspect
the complete canonical registration, regardless of deployment selection.
