# Based on aws-samples/resource-tagging-automation (MIT-0)
#   https://github.com/aws-samples/resource-tagging-automation
# Retains the upstream per-source dispatch and ARN-extraction pattern.
# Extended with: additional services (Redshift, MSK, EKS, ECS, Glue, EMR,
# Step Functions, SageMaker, Secrets Manager, Kinesis, Firehose, Backup, FSx,
# MemoryDB, DocumentDB, API Gateway, CloudFront, Athena, Logs), and with
# derived Owner/CreatedBy/ManagedBy resolution + guardrails in place of the
# upstream static tag map.
import boto3, os, json

# ---------------------------------------------------------------- upstream ---
def aws_ec2(event):
    a=[]; acct=event['account']; reg=event['region']; d=event['detail']; n=d['eventName']
    ec2t=f'arn:aws:ec2:{reg}:{acct}:instance/'; volt=f'arn:aws:ec2:{reg}:{acct}:volume/'
    re_=d.get('responseElements') or {}
    if n=='RunInstances':
        for i in (re_.get('instancesSet') or {}).get('items',[]):
            a.append(ec2t+i['instanceId'])
            try:
                for v in boto3.resource('ec2').Instance(i['instanceId']).volumes.all(): a.append(volt+v.id)
            except Exception as e: print("vol lookup:",e)
    elif n=='CreateVolume' and re_.get('volumeId'): a.append(volt+re_['volumeId'])
    elif n=='AllocateAddress' and re_.get('allocationId'):
        a.append(f"arn:aws:ec2:{reg}:{acct}:elastic-ip/{re_['allocationId']}")
    elif n=='CreateNatGateway':
        g=(re_.get('natGateway') or {}).get('natGatewayId')
        if g: a.append(f'arn:aws:ec2:{reg}:{acct}:natgateway/{g}')
    elif n=='CreateVpcEndpoint':
        e=(re_.get('vpcEndpoint') or {}).get('vpcEndpointId')
        if e: a.append(f'arn:aws:ec2:{reg}:{acct}:vpc-endpoint/{e}')
    return a

def aws_elasticloadbalancing(event):
    re_=event['detail'].get('responseElements') or {}
    return [lb['loadBalancerArn'] for lb in re_.get('loadBalancers',[])] if event['detail']['eventName']=='CreateLoadBalancer' else []

def aws_rds(event):
    re_=event['detail'].get('responseElements') or {}; n=event['detail']['eventName']
    for k in ('dBInstanceArn','dBClusterArn'):
        if n in ('CreateDBInstance','CreateDBCluster') and re_.get(k): return [re_[k]]
    return []

def aws_s3(event):
    rp=event['detail'].get('requestParameters') or {}
    return ['arn:aws:s3:::'+rp['bucketName']] if event['detail']['eventName']=='CreateBucket' and rp.get('bucketName') else []

def aws_lambda(event):
    re_=event['detail'].get('responseElements') or {}
    return [re_['functionArn']] if event['detail']['eventName']=='CreateFunction20150331' and re_.get('functionArn') else []

def aws_dynamodb(event):
    re_=event['detail'].get('responseElements') or {}
    arn=((re_.get('tableDescription') or {}).get('tableArn'))
    return [arn] if event['detail']['eventName']=='CreateTable' and arn else []

def aws_kms(event):
    re_=event['detail'].get('responseElements') or {}
    arn=((re_.get('keyMetadata') or {}).get('arn'))
    return [arn] if event['detail']['eventName']=='CreateKey' and arn else []

def aws_sns(event):
    rp=event['detail'].get('requestParameters') or {}
    return [f"arn:aws:sns:{event['region']}:{event['account']}:{rp['name']}"] if event['detail']['eventName']=='CreateTopic' and rp.get('name') else []

def aws_sqs(event):
    rp=event['detail'].get('requestParameters') or {}
    return [f"arn:aws:sqs:{event['region']}:{event['account']}:{rp['queueName']}"] if event['detail']['eventName']=='CreateQueue' and rp.get('queueName') else []

def aws_elasticfilesystem(event):
    re_=event['detail'].get('responseElements') or {}
    fs=re_.get('fileSystemId')
    return [f"arn:aws:elasticfilesystem:{event['region']}:{event['account']}:file-system/{fs}"] if fs else []

def aws_es(event):
    re_=event['detail'].get('responseElements') or {}
    arn=((re_.get('domainStatus') or {}).get('aRN'))
    return [arn] if arn else []

def aws_elasticache(event):
    a=[]; re_=event['detail'].get('responseElements') or {}; n=event['detail']['eventName']
    reg=event['region']; acct=event['account']
    if n=='CreateReplicationGroup':
        for c in re_.get('memberClusters',[]): a.append(f'arn:aws:elasticache:{reg}:{acct}:cluster:{c}')
    elif n=='CreateCacheCluster' and re_.get('aRN'): a.append(re_['aRN'])
    return a

# ---------------------------------------------------------------- extended ---
def aws_redshift(event):
    re_=event['detail'].get('responseElements') or {}; n=event['detail']['eventName']
    reg=event['region']; acct=event['account']
    if n=='CreateCluster' and re_.get('clusterIdentifier'):
        return [f"arn:aws:redshift:{reg}:{acct}:cluster:{re_['clusterIdentifier']}"]
    if n=='CreateWorkgroup':
        w=(re_.get('workgroup') or {}).get('workgroupArn')
        return [w] if w else []
    if n=='CreateNamespace':
        ns=(re_.get('namespace') or {}).get('namespaceArn')
        return [ns] if ns else []
    return []

def aws_kafka(event):   # Amazon MSK
    re_=event['detail'].get('responseElements') or {}
    for k in ('clusterArn','ClusterArn'):
        if re_.get(k): return [re_[k]]
    return []

def aws_eks(event):
    re_=event['detail'].get('responseElements') or {}
    for path in (('cluster','arn'),('nodegroup','nodegroupArn'),('fargateProfile','fargateProfileArn')):
        v=(re_.get(path[0]) or {}).get(path[1])
        if v: return [v]
    return []

def aws_ecs(event):
    re_=event['detail'].get('responseElements') or {}
    for path in (('cluster','clusterArn'),('service','serviceArn')):
        v=(re_.get(path[0]) or {}).get(path[1])
        if v: return [v]
    return []

def aws_emr(event):
    re_=event['detail'].get('responseElements') or {}
    j=re_.get('jobFlowId')
    return [f"arn:aws:elasticmapreduce:{event['region']}:{event['account']}:cluster/{j}"] if j else []

def aws_states(event):
    re_=event['detail'].get('responseElements') or {}
    return [re_['stateMachineArn']] if re_.get('stateMachineArn') else []

def aws_sagemaker(event):
    re_=event['detail'].get('responseElements') or {}
    for k in ('notebookInstanceArn','endpointArn','domainArn','trainingJobArn','modelArn'):
        if re_.get(k): return [re_[k]]
    return []

def aws_secretsmanager(event):
    re_=event['detail'].get('responseElements') or {}
    return [re_['aRN']] if re_.get('aRN') else []

def aws_kinesis(event):
    rp=event['detail'].get('requestParameters') or {}
    s=rp.get('streamName')
    return [f"arn:aws:kinesis:{event['region']}:{event['account']}:stream/{s}"] if s else []

def aws_firehose(event):
    re_=event['detail'].get('responseElements') or {}
    return [re_['deliveryStreamARN']] if re_.get('deliveryStreamARN') else []

def aws_backup(event):
    re_=event['detail'].get('responseElements') or {}
    return [re_['backupVaultArn']] if re_.get('backupVaultArn') else []

def aws_fsx(event):
    re_=event['detail'].get('responseElements') or {}
    fs=(re_.get('fileSystem') or {}).get('resourceARN')
    return [fs] if fs else []

def aws_memorydb(event):
    re_=event['detail'].get('responseElements') or {}
    c=(re_.get('cluster') or {}).get('aRN')
    return [c] if c else []

def aws_glue(event):
    rp=event['detail'].get('requestParameters') or {}; n=event['detail']['eventName']
    reg=event['region']; acct=event['account']
    if n=='CreateJob' and rp.get('name'):     return [f'arn:aws:glue:{reg}:{acct}:job/{rp["name"]}']
    if n=='CreateCrawler' and rp.get('name'): return [f'arn:aws:glue:{reg}:{acct}:crawler/{rp["name"]}']
    if n=='CreateDatabase':
        d=(rp.get('databaseInput') or {}).get('name')
        return [f'arn:aws:glue:{reg}:{acct}:database/{d}'] if d else []
    return []

def aws_apigateway(event):
    re_=event['detail'].get('responseElements') or {}
    i=re_.get('id')
    return [f"arn:aws:apigateway:{event['region']}::/restapis/{i}"] if i else []

def aws_cloudfront(event):
    re_=event['detail'].get('responseElements') or {}
    d=(re_.get('distribution') or {}).get('aRN')
    return [d] if d else []

def aws_athena(event):
    rp=event['detail'].get('requestParameters') or {}
    w=rp.get('name')
    return [f"arn:aws:athena:{event['region']}:{event['account']}:workgroup/{w}"] if w else []

def aws_logs(event):
    rp=event['detail'].get('requestParameters') or {}
    g=rp.get('logGroupName')
    return [f"arn:aws:logs:{event['region']}:{event['account']}:log-group:{g}"] if g else []

# ------------------------------------------------ resolution + guardrails ---
ssm=boto3.client('ssm'); tagapi=boto3.client('resourcegroupstaggingapi')

def role_map():
    try: return json.loads(ssm.get_parameter(Name='/tagging/role-owner-map')['Parameter']['Value'])
    except Exception: return {}

# Session names that identify an SDK/agent rather than a human.
GENERIC_SESSIONS = ('botocore-session', 'aws-go-sdk', 'aws-sdk', 'AutoScaling',
                    'OrganizationAccountAccessRole', 'AWSCloudFormation')

def looks_like_user(sess):
    """True when the role session name identifies a person."""
    if not sess or sess.startswith('i-'):
        return False
    if sess.startswith(GENERIC_SESSIONS):
        return False
    return '@' in sess or '.' in sess or sess.isalnum()

def resolve(ui):
    """Return (Owner, CreatedBy, ManagedBy).

    Owner resolution prefers the real human identity wherever it is
    discoverable, falling back to the role -> owner map only for machine
    principals:
      1. IAM user            -> userName
      2. Federated user      -> session name (IAM Identity Center / SAML)
      3. Assumed role with a person-like session name -> that session name
      4. Service principal / instance profile -> 'inherit'
      5. Anything else       -> role map, else 'unresolved'
    """
    arn=ui.get('arn') or ''; invoked=ui.get('invokedBy')
    issuer=(ui.get('sessionContext') or {}).get('sessionIssuer') or {}
    created=arn or invoked or ui.get('type','unknown')
    if invoked:                       return ('inherit', created, invoked.split('.')[0])
    if ui.get('type')=='IAMUser':     return (ui.get('userName','unresolved'), created, 'manual')
    sess=arn.split('/')[-1] if '/' in arn else ''
    # Federated: the session name IS the identity store username.
    if 'AWSReservedSSO' in (issuer.get('arn') or '') or 'saml' in (issuer.get('arn') or '').lower():
        return (sess or 'unresolved', created, 'federated')
    if sess.startswith('i-'):         return ('inherit', created, 'instance-profile')
    # Any other assumed role whose session names a person.
    if looks_like_user(sess):         return (sess, created, 'manual')
    # Machine principal: role -> owner map (may pin a specific user).
    return (role_map().get(issuer.get('userName',''), 'unresolved'), created, 'manual')

def current(arn):
    r=tagapi.get_resources(ResourceARNList=[arn]).get('ResourceTagMappingList',[])
    return {t['Key']:t['Value'] for t in r[0]['Tags']} if r else {}

def main(event, context):
    src=event.get('source','').replace('.','_')
    fn=globals().get(src)
    if not fn:
        print(f"UNHANDLED source={event.get('source')} eventName={(event.get('detail') or {}).get('eventName')}")
        return {'statusCode':200,'body':'unhandled source'}
    try: arns=fn(event) or []
    except Exception as e:
        print("extractor error:",e); arns=[]
    ui=(event.get('detail') or {}).get('userIdentity') or {}
    owner, created, managed = resolve(ui)
    print(f"source={event.get('source')} event={(event.get('detail') or {}).get('eventName')} arns={arns} owner={owner}")
    applied=0
    for arn in arns:
        try: cur=current(arn)
        except Exception: cur={}
        if 'ManagedBy' in cur: print("SKIP (ManagedBy set):",arn); continue
        if cur.get('Owner','').endswith('.amazonaws.com'): print("SKIP (AWS-owned):",arn); continue
        tags={'CreatedBy':created[:255],'ManagedBy':managed}
        tags['Owner']= owner if owner!='inherit' else cur.get('Owner','unresolved')
        try:
            tagapi.tag_resources(ResourceARNList=[arn],Tags=tags); applied+=1
            print("TAGGED",arn,tags)
        except Exception as e: print("tag failed",arn,e)
    return {'statusCode':200,'body':json.dumps({'tagged':applied})}
