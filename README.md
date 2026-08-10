# AWS Resource Tagging Automation

Automatically attributes every taggable AWS resource to an accountable owner, so you can answer two questions reliably: **who owns this resource**, and **whose spend is this**.

Derived from [aws-samples/resource-tagging-automation](https://github.com/aws-samples/resource-tagging-automation) (MIT-0). See [NOTICE](NOTICE) for what was retained and what was added.

---

## Why this exists

The widely published auto-tagging pattern — CloudTrail → EventBridge → Lambda applies tags — works, but on its own it leaves most of an estate unattributed.

The resource that appears on your bill is rarely the resource a person created. Someone creates an EKS cluster; AWS creates the node group, which creates an Auto Scaling group, which launches instances, which attach volumes. Only the first action has a human behind it. In a container or Auto Scaling estate, the overwhelming majority of `RunInstances` and `CreateVolume` calls are made by service principals. CloudTrail faithfully records who called the API, and the answer is a machine.

An `Owner` tag derived from that identity is present, compliant, and useless.

This solution handles that by inverting the usual ordering. Ownership flows **downward from parent objects** through native AWS propagation, which is reliable and needs no code. Identity resolution handles only the resources created directly, where a caller identity genuinely identifies an owner.

## How it works

Every resource created by machinery carries a pointer back to the object that created it:

| Pointer tag | Set when |
| --- | --- |
| `aws:cloudformation:stack-name` | Created by a CloudFormation stack |
| `aws:autoscaling:groupName` | Launched by an Auto Scaling group |
| `aws:ec2launchtemplate:id` | Launched from a launch template |
| `eks:nodegroup-name`, `eks:cluster-name` | Part of an EKS node group |

That gives a mechanical test requiring no judgement:

- **Parent pointer present** → the resource was created on someone's behalf → **inherit** ownership from the parent
- **No parent pointer** → created directly → **derive** ownership from the CloudTrail principal

## Components

| Component | Mechanism | Covers |
| --- | --- | --- |
| CloudTrail trail | Multi-Region, management events | Prerequisite for event delivery |
| Event-driven tagger | EventBridge → Lambda, 30+ services | New resources, in seconds |
| Reconciliation sweep | Scheduled Lambda, parent inheritance | Children, backfill, anything missed |
| Role → owner map | SSM Parameter Store | Machine principals |
| Config rule | `REQUIRED_TAGS`, report-only | Compliance visibility |

## Tag taxonomy

| Tag key | Source | Purpose |
| --- | --- | --- |
| `Owner` | Parent object, or resolved principal | Attribution. The reporting dimension |
| `CreatedBy` | CloudTrail principal ARN | Forensic provenance |
| `ManagedBy` | Derived from principal type | Whether safe to modify outside a pipeline |

Set `Owner` to a team or function, not an individual — people move teams while the function remains. Tags are not encrypted and flow into Cost and Usage Report exports, so resolve identities to an internal identifier rather than writing email addresses into tag values.

## Deploy

```bash
aws cloudformation deploy \
  --stack-name resource-tagging \
  --template-file cloudformation/resource-tagging.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    RoleOwnerMap='{"eks-admin-role":"infra-team","deploy-role":"release-eng","AWSServiceRoleForAutoScaling":"inherit"}'
```

### Parameters

| Parameter | Default | Description |
| --- | --- | --- |
| `RoleOwnerMap` | `{"AWSServiceRoleForAutoScaling":"inherit"}` | JSON map of IAM role name → owning team. Use `"inherit"` for roles whose resources take `Owner` from the parent |
| `TrailName` | `resource-tagging-trail` | Name of the multi-Region trail |
| `TrailS3Bucket` | *(blank)* | Existing bucket for trail logs. Blank creates one |
| `SweepSchedule` | `rate(30 minutes)` | How often the reconciliation sweep runs |
| `EnableConfigRule` | `true` | Deploy the AWS Config detective rule |

## Before you deploy

**Verify your CloudTrail actually records management events.** A trail can deliver logs for years while capturing nothing you need — for example, if `IncludeManagementEvents` is `false` and its only selector is a data-event selector for one S3 bucket. Such a trail reports `IsLogging: true`, shows recent successful deliveries, and raises no errors.

```bash
aws cloudtrail get-trail-status   --name <trail-name>
aws cloudtrail get-event-selectors --trail-name <trail-name>
```

This matters more than it appears. **EventBridge does not receive `AWS API Call via CloudTrail` events in an account with no logging trail** — the Lambda deploys cleanly, reports healthy, and tags nothing. This template creates a trail for exactly that reason.

**Check whether your tag keys are already taken.** `Owner` and `ManagedBy` are common enough that another system may own them — AWS itself applies `Owner` to API Gateway VPC Link network interfaces.

```bash
aws resourcegroupstaggingapi get-tag-keys --region <region>
aws resourcegroupstaggingapi get-tag-values --region <region> --key Owner
```

## Supported services

`ec2` · `s3` · `rds` · `lambda` · `dynamodb` · `kms` · `sns` · `sqs` · `elasticfilesystem` · `es` (OpenSearch) · `elasticache` · `elasticloadbalancing` · `redshift` · `redshift-serverless` · `kafka` (MSK) · `amazonmq` · `eks` · `ecs` · `emr` · `states` (Step Functions) · `sagemaker` · `secretsmanager` · `kinesis` · `firehose` · `backup` · `fsx` · `memorydb` · `glue` · `apigateway` · `cloudfront` · `athena` · `logs` · `monitoring`

## Identity resolution

The tagger resolves the CloudTrail principal by type:

Resolution prefers the real human identity wherever it is discoverable, and
falls back to the role map only for machine principals. The **event-driven
tagger and the reconciliation sweep share the same logic**, so a resource
backfilled later gets the same `Owner` it would have received at creation.

| Order | Principal type | `Owner` becomes |
| --- | --- | --- |
| 1 | IAM user | `userIdentity.userName` |
| 2 | Federated user (`AWSReservedSSO` / SAML) | Session name — the identity store username, e.g. `alice@example.com` |
| 3 | Assumed role with a person-like session name | That session name |
| 4 | EC2 instance profile (`role/i-…`) | A controller, not an owner → `inherit` |
| 5 | Service principal (`invokedBy` set) | No human → `inherit`, `ManagedBy` = service |
| 6 | Machine role | SSM role → owner map, else `unresolved` |

`looks_like_user()` rejects generic SDK session names (`botocore-session-*`,
`aws-go-sdk-*`, `AutoScaling`, `i-*`) so an automation session is never mistaken
for a person.

### Pinning a specific user

Federated users resolve to themselves automatically (rule 2). For a machine role
that should be attributed to a named person, map it in `RoleOwnerMap`:

```
RoleOwnerMap='{"ci-deploy-role":"alice@example.com","AWSServiceRoleForAutoScaling":"inherit"}'
```

The sweep re-evaluates any resource whose `Owner` is missing **or**
`unresolved`, so tightening the map fixes previously unattributed resources on
the next pass.

An unmapped principal produces `Owner=unresolved` rather than being skipped, so it surfaces in the coverage metric instead of disappearing.

## Guardrails

- **Skips CloudFormation-managed resources** — writing tags to a resource CloudFormation owns shows up as stack drift, and CloudFormation has no `ignore_tags` equivalent. Both the tagger and the sweep check `DescribeStackResources` and skip resources that are actually in a stack
- **Never overwrites an existing `ManagedBy`** — its presence signals another system manages the resource
- **Skips AWS-owned resources** — any `Owner` value ending in `.amazonaws.com`
- **Never fails closed** — an unresolved principal still gets `CreatedBy` and `ManagedBy`

### Why a tag test is not enough for the drift guardrail

An Auto Scaling group defined in a stack propagates `aws:cloudformation:stack-name`
to every instance it launches, but those instances are **not** stack resources —
CloudFormation manages the ASG, not its instances. Skipping on the mere presence
of that tag would leave every ASG-launched instance and volume unattributed.

`is_stack_managed()` therefore resolves the truth with `DescribeStackResources`
and matches physical IDs. Measured on a stack containing one EC2 instance:

| Resource | In the stack? | Action | Stack drift |
| --- | --- | --- | --- |
| `AWS::EC2::Instance` | yes | **skipped** | `IN_SYNC` |
| attached EBS volume | no — created by EC2 | tagged, `Owner` inherited from the instance | `IN_SYNC` |

Without the guardrail the same stack reported `DRIFTED` within seconds of
creation, caused by the event-driven tagger rather than the sweep.

### Distinguishing how a resource was created

`ManagedBy` plus the parent pointers identify the creation path:

| Path | `ManagedBy` | Parent pointers |
| --- | --- | --- |
| Console / CLI / SDK, direct | `manual` | none |
| CloudFormation, direct stack resource | *(not tagged — skipped)* | `aws:cloudformation:*` |
| Auto Scaling / EKS | `autoscaling` / `eks` | `aws:autoscaling:groupName`, `eks:*` |
| Terraform | `manual` unless `default_tags` sets it | none |

Terraform is the blind spot: CloudTrail sees an assumed deploy role with no
`invokedBy`, so it looks manual. Set `ManagedBy = "terraform"` in `default_tags`
to declare it.

## Known limitations

**`RunInstances` does not return volume IDs**, so the event-driven tagger cannot see an instance's root volume at launch. The sweep inherits it from the attached instance on its next pass. This is why both components are needed — neither alone is sufficient.

**The Resource Groups Tagging API omits resources with no tags at all.** A completely untagged S3 bucket is invisible to `get-resources`. Coverage computed from that API alone will read 100% while untagged resources exist — take the denominator from service-specific `describe-*` calls, or use AWS Config.

**Config is detective, not preventive.** It evaluates resources that already exist. To block untagged deployments you need `AWS::Hooks::GuardHook` on stack operations, tag policies with required tag keys (AWS Organizations), or policy-as-code in CI.

**The AWS Config rule requires an existing configuration recorder.** `EnableConfigRule` defaults to `false` for that reason; enabling it in an account without AWS Config turned on fails the stack with `NoAvailableConfigurationRecorder`.

**Deleting the stack fails while the trail bucket has objects in it.** CloudTrail begins writing within minutes of creation, and CloudFormation cannot delete a non-empty bucket. Empty it first (including object versions) before `delete-stack`.

**`AWS-SetRequiredTags` does not compose with Config remediation as you would expect.** It requires `ResourceARNs` (full ARNs) while Config's `RESOURCE_ID` supplies a bare resource ID. The mismatch produces `Targets: []`, tags nothing, and reports `Success`.

## Ownership for infrastructure as code

For IaC, ownership must be **declared** — CloudTrail records the deploy role, not the owning team, so the information exists nowhere else.

- **CloudFormation** — stack-level tags propagate to supported resources
  ```bash
  aws cloudformation deploy --stack-name x --tags Owner=<team>
  ```
- **Auto Scaling groups** — tag with `PropagateAtLaunch=true`
- **Launch templates** — set `TagSpecifications` for **both** `instance` and `volume`
- **EKS node groups** — tag the node group so it flows to the underlying ASG
- **Terraform** — provider `default_tags` bound to a per-workspace variable, plus `ignore_tags` for `CreatedBy` so Terraform and the tagger do not fight

## Reporting

```bash
# compliance summary
aws configservice describe-compliance-by-config-rule \
  --config-rule-names required-tags-mandatory

# what still needs attention
aws configservice get-compliance-details-by-config-rule \
  --config-rule-name required-tags-mandatory --compliance-types NON_COMPLIANT

# what a team owns
aws resourcegroupstaggingapi get-resources \
  --tag-filters Key=Owner,Values=<team> --query 'ResourceTagMappingList[].ResourceARN'
```

Publish three metrics and treat them as the health of the system:

- **Tag coverage**, per tag and combined
- **Dead-letter queue depth** — any sustained non-zero value means resources are being created unattributed right now
- **Unattributed spend** as a percentage of the monthly bill

## Cost visibility

Activate `Owner` as a cost allocation tag in the management account. Prefer a **Cost Category** mapping `Owner` values to teams over a separate `CostCenter` tag — it is applied at the billing layer, changeable in one place, and applies retroactively.

Enable split cost allocation data for ECS and EKS. Without it, the whole cost of a shared node is attributed to the node's tags rather than distributed across the workloads that consumed it.

## Cleaning up

```bash
aws cloudformation delete-stack --stack-name resource-tagging
```

AWS Config bills per configuration item recorded and CloudTrail bills for log delivery. Both continue until the stack is deleted.

## License

MIT-0. See [LICENSE](LICENSE).
