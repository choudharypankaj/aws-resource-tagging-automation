import os, boto3
REGION=os.environ.get("AWS_REGION","us-west-2"); ACCT=os.environ["ACCT"]
ec2=boto3.client("ec2",region_name=REGION)
asg=boto3.client("autoscaling",region_name=REGION)
cfn=boto3.client("cloudformation",region_name=REGION)
s3=boto3.client("s3",region_name=REGION)
ct=boto3.client("cloudtrail",region_name=REGION)
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

def creator_from_cloudtrail(evname, needle):
    """Last-resort: find who created a resource, from Event history."""
    try:
        tok=None
        for _ in range(6):
            kw=dict(LookupAttributes=[{"AttributeKey":"EventName","AttributeValue":evname}],MaxResults=50)
            if tok: kw["NextToken"]=tok
            r=ct.lookup_events(**kw)
            for e in r.get("Events",[]):
                if needle in e.get("CloudTrailEvent",""):
                    import json as j
                    ui=j.loads(e["CloudTrailEvent"]).get("userIdentity",{})
                    return ui.get("arn") or ui.get("invokedBy") or "unknown"
            tok=r.get("NextToken")
            if not tok: break
    except Exception as ex: print("ct lookup failed",ex)
    return None

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
        if t.get("Owner") in (None,"unresolved"):
            o=asg_owner(t["aws:autoscaling:groupName"]) if t.get("aws:autoscaling:groupName") else None
            if not o and t.get("aws:cloudformation:stack-name"):
                o=stack_owner(t["aws:cloudformation:stack-name"])
            if not o: o=creator_from_cloudtrail("RunInstances",iid) and "unresolved"
            if o: need["Owner"]=o
        if "CreatedBy" not in t:
            c=creator_from_cloudtrail("RunInstances",iid)
            if c: need["CreatedBy"]=c[:255]
        if "ManagedBy" not in t:
            need["ManagedBy"]="asg" if t.get("aws:autoscaling:groupName") else "manual"
        fixed+=apply(f"arn:aws:ec2:{REGION}:{ACCT}:instance/{iid}",need,"FIX instance")
        if need: t.update(need)
    # 2. volumes inherit from attached instance
    for v in ec2.describe_volumes()["Volumes"]:
        vt=tags_of(v.get("Tags")); need={}
        parent=next((a.get("InstanceId") for a in v.get("Attachments",[])),None)
        pt=inst.get(parent,{})
        for k in REQUIRED:
            if k not in vt and pt.get(k): need[k]=pt[k]
        fixed+=apply(f"arn:aws:ec2:{REGION}:{ACCT}:volume/{v['VolumeId']}",need,"FIX volume")
    # 3. S3 buckets
    for b in s3.list_buckets()["Buckets"]:
        name=b["Name"]
        try: bt={t["Key"]:t["Value"] for t in s3.get_bucket_tagging(Bucket=name)["TagSet"]}
        except Exception: bt={}
        need={}
        if "CreatedBy" not in bt:
            c=creator_from_cloudtrail("CreateBucket",name)
            need["CreatedBy"]=(c or "pre-existing")[:255]
        if "Owner" not in bt: need["Owner"]="platform-engineering"
        if "ManagedBy" not in bt: need["ManagedBy"]="manual"
        fixed+=apply(f"arn:aws:s3:::{name}",need,"FIX bucket")
    print(f"SWEEP complete: {fixed} resources updated")
    return {"fixed":fixed}
