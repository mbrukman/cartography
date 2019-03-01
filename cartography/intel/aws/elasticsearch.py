import logging
from cartography.intel.dns import ingest_dns_record_by_fqdn
from cartography.util import run_cleanup_job
from policyuniverse.policy import Policy
import json

logger = logging.getLogger(__name__)

# TODO get this programmatically
# https://docs.aws.amazon.com/general/latest/gr/rande.html#elasticsearch-service-regions
es_regions = [
    'us-east-2',
    'us-east-1',
    'us-west-1',
    'us-west-2',
    'ap-northeast-1',
    'ap-northeast-2',
    'ap-south-1',
    'ap-southeast-1',
    'ca-central-1',
    # 'cn-northwest-1',  -- intentionally ignored. need specific token
    'eu-central-1',
    'eu-west-1',
    'eu-west-2',
    'eu-west-3',
    'sa-east-1',
    # 'us-gov-west-1', -- intentionally ignored, need specific token
]


def _get_es_domains(client):
    """
    Get ES domains.

    :param client: ES boto client
    :return: list of ES domains
    """
    data = client.list_domain_names()
    domain_names = [d['DomainName'] for d in data.get('DomainNames', [])]
    # NOTE describe_elasticsearch_domains takes at most 5 domain names
    domain_name_chunks = [domain_names[i:i + 5] for i in range(0, len(domain_names), 5)]
    domains = []
    for domain_name_chunk in domain_name_chunks:
        chunk_data = client.describe_elasticsearch_domains(DomainNames=domain_name_chunk)
        domains.extend(chunk_data['DomainStatusList'])
    return domains


def _load_es_domains(session, domain_list, aws_account_id, aws_update_tag):
    """
    Ingest Elastic Search domains

    :param session: Neo4j session object
    :param aws_account_id: The AWS account related to the domains
    :param domains: Domain list to ingest
    """
    ingest_records = """
    UNWIND {Records} as record
    MERGE (es:ESDomain{id: record.DomainId})
    ON CREATE SET es.firstseen = timestamp(), es.arn = record.ARN, es.domainid = record.DomainId
    SET es.lastupdated = {aws_update_tag}, es.deleted = record.Deleted, es.created = record.created,
    es.endpoint = record.Endpoint, es.elasticsearch_version = record.ElasticsearchVersion,
    es.elasticsearch_cluster_config_instancetype = record.ElasticsearchClusterConfig.InstanceType,
    es.elasticsearch_cluster_config_instancecount = record.ElasticsearchClusterConfig.InstanceCount,
    es.elasticsearch_cluster_config_dedicatedmasterenabled = record.ElasticsearchClusterConfig.DedicatedMasterEnabled,
    es.elasticsearch_cluster_config_zoneawarenessenabled = record.ElasticsearchClusterConfig.ZoneAwarenessEnabled,
    es.elasticsearch_cluster_config_dedicatedmastertype = record.ElasticsearchClusterConfig.DedicatedMasterType,
    es.elasticsearch_cluster_config_dedicatedmastercount = record.ElasticsearchClusterConfig.DedicatedMasterCount,
    es.ebs_options_ebsenabled = record.EBSOptions.EBSEnabled,
    es.ebs_options_volumetype = record.EBSOptions.VolumeType,
    es.ebs_options_volumesize = record.EBSOptions.VolumeSize,
    es.ebs_options_iops = record.EBSOptions.Iops,
    es.encryption_at_rest_options_enabled = record.EncryptionAtRestOptions.Enabled,
    es.encryption_at_rest_options_kms_key_id = record.EncryptionAtRestOptions.KmsKeyId,
    es.log_publishing_options_cloudwatch_log_group_arn = record.LogPublishingOptions.CloudWatchLogsLogGroupArn,
    es.log_publishing_options_enabled = record.LogPublishingOptions.Enabled
    WITH es
    MATCH (account:AWSAccount{id: {AWS_ACCOUNT_ID}})
    MERGE (account)-[r:RESOURCE]->(es)
    ON CREATE SET r.firstseen = timestamp()
    SET r.lastupdated = {aws_update_tag}
    """

    # TODO this is a hacky workaround -- neo4j doesn't accept datetime objects and this section of the object
    # TODO contains one. we really shouldn't be sending the entire object to neo4j
    for d in domain_list:
        del d['ServiceSoftwareOptions']

    session.run(
        ingest_records,
        Records=domain_list,
        AWS_ACCOUNT_ID=aws_account_id,
        aws_update_tag=aws_update_tag
    )

    for domain in domain_list:
        domain_id = domain["DomainId"]
        _link_es_domains_to_dns(session, domain_id, domain, aws_update_tag)
        _link_es_domain_vpc(session, domain_id, domain, aws_update_tag)
        _process_access_policy(session, domain_id, domain)


def _link_es_domains_to_dns(session, domain_id, domain_data, aws_update_tag):
    """
    Link the ES domain to its DNS FQDN endpoint and create associated nodes in the graph
    if needed

    :param session: Neo4j session object
    :param domain_id: ES domain id
    :param domain_data: domain data
    """
    # TODO add support for endpoints to this method
    if domain_data.get("Endpoint"):
        ingest_dns_record_by_fqdn(session, aws_update_tag, domain_data["Endpoint"], domain_id)
    else:
        logger.debug("No es endpoint data for domain id {0}".format(domain_id))


def _link_es_domain_vpc(session, domain_id, domain_data, aws_update_tag):
    """
    Link the ES domain to its DNS FQDN endpoint and create associated nodes in the graph
    if needed

    :param session: Neo4j session object
    :param domain_id: ES domain id
    :param domain_data: domain data
    """
    ingest_subnet = """
    MATCH (es:ESDomain{id: {DomainId}})
    WITH es
    UNWIND {SubnetList} as subnet_id
        MATCH (subnet_node:EC2Subnet{id: subnet_id})
        MERGE (es)-[r:PART_OF_SUBNET]->(subnet_node)
        ON CREATE SET r.firstseen = timestamp()
        SET r.lastupdated = {aws_update_tag}
    """

    ingest_sec_groups = """
    MATCH (es:ESDomain{id: {DomainId}})
    WITH es
    UNWIND {SecGroupList} as ecsecgroup_id
        MATCH (group_node:EC2SecurityGroup{id: ecsecgroup_id})
        MERGE (es)-[r:MEMBER_OF_EC2_SECURITY_GROUP]->(group_node)
        ON CREATE SET r.firstseen = timestamp()
        SET r.lastupdated = {aws_update_tag}
    """
    # TODO we really shouldn't be sending full objects to Neo4j
    if domain_data.get("VPCOptions"):
        vpc_data = domain_data["VPCOptions"]
        subnetList = vpc_data.get("SubnetIds", [])
        groupList = vpc_data.get("SecurityGroupIds", [])

        if len(subnetList) > 0:
            session.run(
                ingest_subnet,
                DomainId=domain_id,
                SubnetList=subnetList,
                aws_update_tag=aws_update_tag
            )

        if len(groupList) > 0:
            session.run(
                ingest_sec_groups,
                DomainId=domain_id,
                SecGroupList=groupList,
                aws_update_tag=aws_update_tag
            )


def _process_access_policy(session, domain_id, domain_data):
    """
    Link the ES domain to its DNS FQDN endpoint and create associated nodes in the graph
    if needed

    :param session: Neo4j session object
    :param domain_id: ES domain id
    :param domain_data: domain data
    """
    tag_es = "MATCH (es:ESDomain{id: {DomainId}}) SET es.exposed_internet = {InternetExposed}"

    exposed_internet = False

    if domain_data.get("Endpoint") and domain_data.get("AccessPolicies"):
        policy = Policy(json.loads(domain_data['AccessPolicies']))
        if policy.is_internet_accessible():
            exposed_internet = True

    session.run(tag_es, DomainId=domain_id, InternetExposed=exposed_internet)


def cleanup(session, update_tag, aws_account_id):
    run_cleanup_job(
        'aws_import_es_cleanup.json',
        session,
        {'UPDATE_TAG': update_tag, 'AWS_ID': aws_account_id}
    )


def sync(neo4j_session, boto3_session, aws_account_id, update_tag):
    for region in es_regions:
        logger.info("Syncing Elasticsearch Service for region '%s' in account '%s'.", region, aws_account_id)
        client = boto3_session.client('es', region_name=region)
        data = _get_es_domains(client)
        _load_es_domains(neo4j_session, data, aws_account_id, update_tag)

    cleanup(neo4j_session, update_tag, aws_account_id)