# M8 CUR 2.0 manual setup and schema verification

This is preparatory documentation only. `BillingStack` creates private CUR and
Athena result buckets, a Glue database and a scan-limited workgroup; it does not
create a billing Data Export. No billing configuration or AWS resource has been
changed by this work.

Before a separately approved live billing task:

1. In the AWS Billing and Cost Management console in **us-east-1**, create a
   Cost and Usage Report 2.0 Data Export to the synthesized
   `CurExportsBucketName` output. Select hourly time granularity and Parquet
   output. Record the export name, destination prefix, account ID, output bucket
   ARN, and delivery role/policy resource IDs in the deployment record.
   Configure the output bucket exactly as required by the console and verify
   that delivery succeeds.
2. Only after explicit approval, launch one short-lived throwaway instance with
   the full controller ownership tags (`managed-by`, `reservation-id`, `team`,
   `project`, `env`). Record the instance and attached EBS volume IDs. Terminate
   the instance and confirm its EBS volume is deleted after the export has had
   time to deliver.
3. Activate the controller's cost allocation tags in Billing. Tag activation
   and first appearance in CUR may be delayed; timing is account-specific and
   must be observed. **VERIFY:** record activation timestamp, first CUR row
   timestamp, and attribution completeness. Do not assume immediate tagging.
4. Inspect the delivered Parquet schema and representative rows in Athena using
   the `BillingAthenaWorkgroupName` and `BillingGlueDatabaseName` outputs.
   Confirm actual column names, resource IDs, usage types, line item types,
   tag-column encoding and period fields before authoring reconciliation SQL.
5. Keep a redacted sample schema and query result with the verification record.
   Do not build reconciliation rules until schema and attribution are verified.

The workgroup enforces a 1 GiB per-query scan cap. Both buckets block public
access, use S3-managed encryption, and apply lifecycle expiration. This setup
does not demonstrate Data Export delivery, tag activation, Athena access, or
billing attribution.
