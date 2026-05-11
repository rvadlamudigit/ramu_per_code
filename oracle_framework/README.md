# oracle_framework

Reusable Oracle -> CSV -> S3 extraction framework, fully driven by a YAML
config. Streams query results through Python generators so very large
tables don't blow memory, splits output into chunked CSV files, and
uploads them to S3 with optional SSE-KMS encryption.

## What it does

1. Reads all metadata from a YAML file (DB connection, SQL, output
   layout, S3 target, KMS key).
2. Connects to Oracle. Authentication can be plaintext (in YAML) or
   fetched from AWS Secrets Manager.
3. Executes the configured SQL and streams results through a generator
   (`cursor.fetchmany`) so memory stays flat regardless of row count.
4. Writes results to one or more CSV files on local disk, splitting at
   a configurable row count: `<base>_1.csv`, `<base>_2.csv`, ...
5. Uploads each file to an S3 bucket with optional SSE-KMS encryption.
6. Deletes local files after a successful upload.

## Layout

```
oracle_framework/
├── README.md
├── requirements.txt
├── oracle_to_s3.py          # main class: OracleToS3Extract
├── runner.py                # CLI entry: python runner.py <yaml>
└── config/
    └── extract_example.yaml # commented sample config
```

## Setup

```
pip install -r requirements.txt
```

`oracledb` runs in pure-Python "thin" mode by default, so no Oracle
Instant Client is required. If you need thick mode (legacy features,
advanced security), install Instant Client and call
`oracledb.init_oracle_client(lib_dir=...)` before constructing the job.

## AWS credentials

The framework uses boto3's default credential chain (env vars, shared
config files, EC2/EKS instance profile, etc.). You do not put AWS
access keys in the YAML. The optional `secret_name` value is the only
AWS-related field needed in the config.

## Run

```
python runner.py config/extract_example.yaml
python runner.py -v   config/extract_example.yaml   # stdlib DEBUG only
python runner.py -d   config/extract_example.yaml   # full debug mode
```

### Debug mode (`-d` / `--debug`)

Full framework debug mode enables, on top of plain `--verbose`:

- A redacted dump of the effective YAML config (passwords / KMS key /
  secret values are masked).
- Per-batch row counts, fetch timings, and a periodic INFO heartbeat
  every 10 batches.
- Per-file row counts and on-disk byte sizes after each CSV is closed.
- Per-upload throughput (MiB/s) and a phase summary for the S3 step.
- An end-of-run metrics summary
  (`rows / batches / files / bytes written / bytes uploaded / phase timings`).
- A `.debug` log file alongside `.log` / `.error` / `.critical`
  containing the full DEBUG firehose. The main `.log` keeps its
  normal verbosity, so existing log-shipping pipelines are unaffected.
- A richer formatter that includes `filename:lineno` and thread name
  so every log line is traceable back to source.

Or use it as a library:

```python
from oracle_to_s3 import OracleToS3Extract
from runner import validate_config

job = OracleToS3Extract("config/my_extract.yaml")

# Step-by-step (useful for testing or custom flows):
cfg     = job.load_config()        # Step 1a: read + parse YAML
validate_config(cfg)               # Step 1b: schema check (runner-owned)
job.connect()
batches = job.execute_query()      # generator
files   = job.write_csv_files(batches)
uris    = job.upload_to_s3(files)
job.cleanup_local(files)
job.close()
```

> Schema validation lives in `runner.py` (`validate_config`), not in the
> `OracleToS3Extract` class. The framework class is a pure toolkit; the
> runner owns orchestration and validation. Library callers that
> bypass the runner should call `validate_config(cfg)` themselves
> immediately after `load_config()`.

## YAML reference

See `config/extract_example.yaml` for a fully commented example.

Required keys:

- `oracle.dsn`
- `oracle.auth_method` (`plain` or `aws_secret`)
- For `plain`: `oracle.user`, `oracle.password`
- For `aws_secret`: `oracle.secret_name`
- `sql`
- `output.local_dir`, `output.base_filename`, `output.records_per_file`
- `s3.bucket`

Optional keys:

- `output.csv.delimiter` (default `,`)
- `output.csv.include_header` (default `true`)
- `output.csv.quote_all` (default `false`)
- `fetch.array_size` (default `5000`)
- `s3.prefix`
- `s3.kms_key_id` (enables SSE-KMS)
- `s3.aws_region`
- `oracle.aws_region` (only used when auth_method is `aws_secret`)
- `cleanup_local` (default `true`)

## Notes

- `records_per_file` controls how many rows go in each CSV file. The
  framework opens the next file as soon as the current one reaches
  this count, so memory usage is bounded regardless of total result
  size.
- `fetch.array_size` controls how many rows the Oracle driver pulls
  per round-trip. Increase for throughput, decrease for memory.
- KMS encryption is opt-in: if `s3.kms_key_id` is omitted the upload
  uses whatever bucket-default encryption is configured.
- On any failure (Oracle connection, query, S3 upload), the framework
  raises `OracleToS3ExtractError` with the underlying error chained.
  Local files are kept for inspection if the failure happens before
  cleanup.
