"""Custom AWS Config rule: Owner must be present AND meaningful.

REQUIRED_TAGS only checks that a key exists, so Owner=unresolved passes it.
That is exactly the state produced when an Auto Scaling group or a
CloudFormation stack was never given an Owner to propagate -- and for
CloudFormation-managed resources the tagger deliberately will not fix it,
because writing the tag would cause stack drift. Detection is the only control
available for that population, so it has to be accurate.
"""
import json, boto3
config = boto3.client('config')
BAD_VALUES = {'', 'unresolved', 'unknown', 'none', 'n/a', 'tbd'}
REQUIRED = ('Owner', 'CreatedBy', 'ManagedBy')

def evaluate(ci):
    tags = ci.get('tags') or {}
    missing = [k for k in REQUIRED if k not in tags]
    placeholder = [k for k in REQUIRED if tags.get(k, '').strip().lower() in BAD_VALUES]
    if missing or placeholder:
        parts = []
        if missing: parts.append("missing: " + ", ".join(missing))
        if placeholder: parts.append("placeholder value: " + ", ".join(placeholder))
        # Name the parent so the finding is actionable
        parent = (tags.get('aws:cloudformation:stack-name')
                  or tags.get('aws:autoscaling:groupName')
                  or tags.get('eks:nodegroup-name'))
        if parent: parts.append(f"declare Owner on parent '{parent}'")
        return 'NON_COMPLIANT', "; ".join(parts)[:255]
    return 'COMPLIANT', 'Owner, CreatedBy and ManagedBy present and meaningful'

def handler(event, context):
    invoking = json.loads(event['invokingEvent'])
    ci = invoking.get('configurationItem') or invoking.get('configurationItemSummary') or {}
    if ci.get('configurationItemStatus') in ('ResourceDeleted', 'ResourceNotRecorded'):
        compliance, note = 'NOT_APPLICABLE', 'resource deleted'
    else:
        compliance, note = evaluate(ci)
    print(f"{ci.get('resourceType')} {ci.get('resourceId')} -> {compliance}: {note}")
    config.put_evaluations(
        Evaluations=[{
            'ComplianceResourceType': ci['resourceType'],
            'ComplianceResourceId': ci['resourceId'],
            'ComplianceType': compliance,
            'Annotation': note,
            'OrderingTimestamp': ci['configurationItemCaptureTime'],
        }],
        ResultToken=event['resultToken'])
    return compliance
