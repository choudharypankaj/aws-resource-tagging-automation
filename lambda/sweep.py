import os, json, boto3
REGION=os.environ.get("AWS_REGION","us-west-2"); ACCT=os.environ["ACCT"]
ec2=boto3.client("ec2",region_name=REGION)
asg=boto3.client("autoscaling",region_name=REGION)
cfn=boto3.client("cloudformation",region_name=REGION)

_stackres_cache={}

def is_stack_managed(arn, tags):
    """True when CloudFormation *directly* manages this resource.

    Per the design doc: the automation must skip stack-managed resources,
    because CloudFormation has no ignore_tags equivalent and out-of-band tags
    show up as stack drift. ASG-launched instances inherit the stack tags by
    propagation but are NOT stack resources, so they are still fair game --
    hence the DescribeStackResources check rather than a tag-presence test.
    """
    stack = tags.get('aws:cloudformation:stack-name')
    if not stack:
        return False
    rid = arn.split('/')[-1] if '/' in arn else arn.split(':')[-1]
    if stack not in _stackres_cache:
        try:
            _stackres_cache[stack] = {
                r['PhysicalResourceId']
                for r in cfn.describe_stack_resources(StackName=stack)['StackResources']
            }
        except Exception as e:
            print('stack lookup failed', stack, e)
            _stackres_cache[stack] = set()
    return rid in _stackres_cache[stack]

s3=boto3.client("s3",region_name=REGION)
ct=boto3.client("cloudtrail",region_name=REGION)
ssm=boto3.client("ssm",region_name=REGION)
tagapi=boto3.client("resourcegroupstaggingapi",region_name=REGION)
REQUIRED=("Owner","CreatedBy","ManagedBy")

def tags_of(l): return {t["Key"]:t["Value"] for t in (l or [])}

def stack_owner(name,cache={}):
    if name in cache: return cache[name]
    try:
        t={x["Key"]:x["Value"] for x in cfn.describe_stacks(StackName=name)["Stacks"][0].get("Tags",[])}
        cache[name]=t.get("Owner")
    except Exception: cache[name]=None
    return cache[name]

def asg_owner(name,cache={}):
    if name in cache: return cache[name]
    try:
        t=asg.describe_tags(Filters=[{"Name":"auto-scaling-group","Values":[name]},
                                     {"Name":"key","Values":["Owner"]}])["Tags"]
        cache[name]=t[0]["Value"] if t else None
    except Exception: cache[name]=None
    return cache[name]

def identity_from_cloudtrail(evname, needle):
    """Return the userIdentity block of the event that created `needle`."""
    try:
        tok=None
        for _ in range(6):
            kw=dict(LookupAttributes=[{"AttributeKey":"EventName","AttributeValue":evname}],MaxResults=50)
            if tok: kw["NextToken"]=tok
            r=ct.lookup_events(**kw)
            for e in r.get("Events",[]):
                if needle in e.get("CloudTrailEvent",""):
                    import json as j
                    return j.loads(e["CloudTrailEvent"]).get("userIdentity",{})
            tok=r.get("NextToken")
            if not tok: break
    except Exception as ex: print("ct lookup failed",ex)
    return None

GENERIC_SESSIONS = ('botocore-session','aws-go-sdk','aws-sdk','AutoScaling',
                    'OrganizationAccountAccessRole','AWSCloudFormation')

def role_map():
    try: return json.loads(ssm.get_parameter(Name="/tagging/role-owner-map")["Parameter"]["Value"])
    except Exception: return {}

def looks_like_user(sess):
    if not sess or sess.startswith('i-'): return False
    if sess.startswith(GENERIC_SESSIONS): return False
    return '@' in sess or '.' in sess or sess.isalnum()

def resolve(ui):
    """Same resolution the event-driven tagger uses."""
    if not ui: return (None, None, None)
    arn=ui.get('arn') or ''; invoked=ui.get('invokedBy')
    issuer=(ui.get('sessionContext') or {}).get('sessionIssuer') or {}
    created=arn or invoked or ui.get('type','unknown')
    if invoked: return ('inherit', created, invoked.split('.')[0])
    if ui.get('type')=='IAMUser': return (ui.get('userName','unresolved'), created, 'manual')
    sess=arn.split('/')[-1] if '/' in arn else ''
    if 'AWSReservedSSO' in (issuer.get('arn') or '') or 'saml' in (issuer.get('arn') or '').lower():
        return (sess or 'unresolved', created, 'federated')
    if sess.startswith('i-'): return ('inherit', created, 'instance-profile')
    if looks_like_user(sess): return (sess, created, 'manual')
    return (role_map().get(issuer.get('userName',''), 'unresolved'), created, 'manual')

def apply(arn,tags,why):
    if not tags: return 0
    tagapi.tag_resources(ResourceARNList=[arn],Tags=tags)
    print(f"{why} {arn} {tags}"); return 1

def main(event, context):
    fixed=0
    inst={}
    for r in ec2.describe_instances()["Reservations"]:
        for i in r["Instances"]:
            if i["State"]["Name"] in ("terminated","shutting-down"): continue
            inst[i["InstanceId"]]=tags_of(i.get("Tags"))
    # 1. instances
    for iid,t in inst.items():
        need={}
        if is_stack_managed(f"arn:aws:ec2:{REGION}:{ACCT}:instance/{iid}", t):
            print("SKIP (CloudFormation-managed):", iid); continue
        ui = None
        if t.get("Owner") in (None,"unresolved") or "CreatedBy" not in t or "ManagedBy" not in t:
            ui = identity_from_cloudtrail("RunInstances", iid)
        owner, created, managed = resolve(ui) if ui else (None,None,None)
        if t.get("Owner") in (None,"unresolved"):
            o=None
            if t.get("aws:autoscaling:groupName"): o=asg_owner(t["aws:autoscaling:groupName"])
            if not o and t.get("aws:cloudformation:stack-name"): o=stack_owner(t["aws:cloudformation:stack-name"])
            if not o and owner and owner!="inherit": o=owner
            if o and o!=t.get("Owner"): need["Owner"]=o
        if "CreatedBy" not in t and created: need["CreatedBy"]=created[:255]
        if "ManagedBy" not in t:
            need["ManagedBy"]= "asg" if t.get("aws:autoscaling:groupName") else (managed or "manual")
        fixed+=apply(f"arn:aws:ec2:{REGION}:{ACCT}:instance/{iid}",need,"FIX instance")
        if need: t.update(need)

    # 2. volumes inherit from attached instance
    for v in ec2.describe_volumes()["Volumes"]:
        vt=tags_of(v.get("Tags")); need={}
        parent=next((a.get("InstanceId") for a in v.get("Attachments",[])),None)
        pt=inst.get(parent,{})
        for k in REQUIRED:
            stale = vt.get(k) in (None, "unresolved")
            if stale and pt.get(k) and pt[k] != "unresolved" and pt[k] != vt.get(k):
                need[k]=pt[k]
        fixed+=apply(f"arn:aws:ec2:{REGION}:{ACCT}:volume/{v['VolumeId']}",need,"FIX volume")
    # 3. S3 buckets
    for b in s3.list_buckets()["Buckets"]:
        name=b["Name"]
        try: bt={t["Key"]:t["Value"] for t in s3.get_bucket_tagging(Bucket=name)["TagSet"]}
        except Exception: bt={}
        need={}
        ui=identity_from_cloudtrail("CreateBucket",name) if ("CreatedBy" not in bt or bt.get("Owner") in (None,"unresolved")) else None
        bowner,bcreated,bmanaged=resolve(ui) if ui else (None,None,None)
        if "CreatedBy" not in bt: need["CreatedBy"]=(bcreated or "pre-existing")[:255]
        if bt.get("Owner") in (None,"unresolved") and bowner and bowner!="inherit": need["Owner"]=bowner
        if "ManagedBy" not in bt: need["ManagedBy"]=bmanaged or "manual"
        fixed+=apply(f"arn:aws:s3:::{name}",need,"FIX bucket")
    print(f"SWEEP complete: {fixed} resources updated")
    return {"fixed":fixed}
