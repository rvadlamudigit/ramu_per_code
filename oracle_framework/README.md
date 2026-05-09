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
```

Or use it as a library:

```python
from oracle_to_s3 import OracleToS3Extract

job = OracleToS3Extract("config/my_extract.yaml")

# Step-by-step (useful for testing or custom flows):
job.connect()
batches = job.execute_query()      # generator
files   = job.write_csv_files(batches)
uris    = job.upload_to_s3(files)
job.cleanup_local(files)
job.close()

# Or just:
uris = job.run()
```

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
