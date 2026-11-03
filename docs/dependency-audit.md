# Dependency audit

The dependency audit fails on known advisories by default. One upstream
constraint is currently excepted:

- [`PYSEC-2026-132`](https://osv.dev/vulnerability/PYSEC-2026-132) affects
  `simpleeval` versions before 1.0.5. The pinned Jaxonomy 3.4.0 runtime, which
  is required to run the Jaxonomy Python FMU, constrains SimpleEval to `~=0.9`
  and resolves to 0.9.13. The vulnerable evaluator is used by Jaxonomy's
  optimization CLI; FMU Executor does not invoke that CLI or pass external
  names/callbacks to SimpleEval.

The workflow ignores only this advisory. It remains a known dependency risk,
not a claim that the installed package is patched. Remove the exception when
Jaxonomy releases a compatible dependency bound or another safe resolution is
available; keep all other audit findings blocking.
