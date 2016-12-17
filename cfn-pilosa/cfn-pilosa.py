from __future__ import print_function
from troposphere import route53, iam, ec2, Ref, Template, GetAtt, Base64, Parameter, Tags, Sub
from troposphere.iam import Role, InstanceProfile
from troposphere_sugar.decorators import cfparam, cfresource
from troposphere_sugar import Skel
from troposphere import route53, Ref, Parameter, Join
from awacs.aws import Allow, Statement, Principal, Policy
from awacs.sts import AssumeRole
from functools import partial
from textwrap import dedent
import sys

class PilosaTemplate(Skel):
    def __init__(self, cluster_size, num_agents):
        super(PilosaTemplate, self).__init__()
        self.cluster_size = cluster_size
        self.num_agents = num_agents

    @cfparam
    def vpc(self):
        return Parameter(
            'VPC',
            Description='VPC to use for pilosa instance',
            Type='String',
        )

    @cfparam
    def subnet(self):
        return Parameter(
            'Subnet',
            Description='Subnet to use for pilosa instance',
            Type='String',
        )

    @cfparam
    def ami(self):
        return Parameter(
            'AMI',
            Description='AMI to use for pilosa instance',
            Type='String',
            Default='ami-e3c3b8f4',
        )

    @cfparam
    def key_pair(self):
        return Parameter(
            'KeyPair',
            Description='Key pair to use for sudoer user',
            Type='String',
        )

    @cfparam
    def instance_type(self):
        return Parameter(
            'InstanceType',
            Description='Instance type of pilosa',
            Type='String',
            Default='m3.medium',
        )

    @cfparam
    def cluster_name(self):
        return Parameter(
            'ClusterName',
            Description='Unique name for this pilosa cluster. Used in DNS (pilosa0.{{name}}.sandbox.pilosa.com',
            Type='String',
            Default='cluster0',
        )

    @cfresource
    def role(self):
        return Role(
            "PilosaRole",
            AssumeRolePolicyDocument=Policy(
                Statement=[
                    Statement(
                        Effect=Allow,
                        Action=[AssumeRole],
                        Principal=Principal("Service", ["ec2.amazonaws.com"])
                    )
                ]
            ),
            Policies=[iam.Policy(
                PolicyName='PilosaS3Policy',
                PolicyDocument={
                  "Version": "2012-10-17",
                  "Statement": [
                    {
                      "Effect": "Allow",
                      "Action": ["s3:*"],
                      "Resource": ["arn:aws:s3:::dist.pilosa.com", "arn:aws:s3:::dist.pilosa.com/*"]
                    }
                  ]
                }
            )],
        )

    @cfresource
    def instance_profile(self):
        return InstanceProfile(
            "PilosaInstanceProfile",
            Roles=[Ref(self.role)]
        )

    @cfresource
    def hosted_zone(self):
        return route53.HostedZone(
            'PilosaZone',
            Name=Join('', [Ref(self.cluster_name), '.sandbox.pilosa.com']),
            VPCs=[route53.HostedZoneVPCs(VPCId=Ref(self.vpc), VPCRegion=Ref('AWS::Region'))])

    @cfresource
    def instance_security_group(self):
        return ec2.SecurityGroup(
            'PilosaInstanceSecurityGroup',
            GroupDescription='Enable SSH access via port 22',
            SecurityGroupIngress=[
                ec2.SecurityGroupRule(
                    IpProtocol='tcp',
                    FromPort='22',
                    ToPort='22',
                    CidrIp='0.0.0.0/0',
                ),
            ],
            VpcId=Ref(self.vpc),
        )

    @cfresource
    def instance_security_group_ingress(self):
        return ec2.SecurityGroupIngress(
            "PilosaIngress",
            IpProtocol='tcp',
            FromPort='15000',
            ToPort='15000',
            GroupId=Ref(self.instance_security_group),
            SourceSecurityGroupId=Ref(self.instance_security_group),
        )

    def instance(self, index):
        config_file = dedent('''
            data-dir = "/tmp/pil0"
            host = "pilosa{node}.{stack_name}.sandbox.pilosa.com:15000"

            [cluster]
            replicas = {count}

            ''')[1:].format(node=index, count=self.cluster_size, stack_name='${AWS::StackName}')

        for node in range(self.cluster_size):
            config_file += dedent('''
                [[cluster.node]]
                host = "pilosa{node}.{stack_name}.sandbox.pilosa.com:15000"

                '''[1:]).format(node=node, stack_name='${AWS::StackName}')

        user_data = dedent('''
                #!/bin/bash
                apt install -y awscli
                aws s3 cp s3://dist.pilosa.com/2.0.0/pilosa /usr/local/bin/
                chmod +x /usr/local/bin/pilosa

                cat > /etc/pilosa.cfg << EOF
                {config_file}
                EOF

                '''[1:]).format(config_file=config_file)

        return ec2.Instance(
            'PilosaInstance{}'.format(index),
            ImageId = Ref(self.ami), #ubuntu
            InstanceType = Ref(self.instance_type),
            KeyName = Ref(self.key_pair),
            IamInstanceProfile=Ref(self.instance_profile),
            NetworkInterfaces=[
                ec2.NetworkInterfaceProperty(
                    GroupSet=[Ref(self.instance_security_group.title)],
                    AssociatePublicIpAddress='true',
                    DeviceIndex='0',
                    DeleteOnTermination='true',
                    SubnetId=Ref(self.subnet))],

            UserData = Base64(Sub(user_data)),
        )

    def agent_instance(self, index):
        user_data = dedent('''
                #!/bin/bash
                apt install -y awscli
                aws s3 cp s3://dist.pilosa.com/2.0.0/pilosactl /usr/local/bin/
                chmod +x /usr/local/bin/pilosactl
                '''[1:])
        return ec2.Instance(
            'PilosaAgentInstance{}'.format(index),
            ImageId=Ref(self.ami), # ubuntu
            InstanceType=Ref(self.instance_type),
            KeyName=Ref(self.key_pair),
            IamInstanceProfile=Ref(self.instance_profile),
            NetworkInterfaces=[
                ec2.NetworkInterfaceProperty(
                    GroupSet=[Ref(self.instance_security_group.title)],
                    AssociatePublicIpAddress='true',
                    DeviceIndex='0',
                    DeleteOnTermination='true',
                    SubnetId=Ref(self.subnet))],

            UserData=Base64(Sub(user_data)),
        )

    def public_record_set(self, index):
        return route53.RecordSetType(
            'PilosaPublicRecordSet{}'.format(index),
            HostedZoneName='sandbox.pilosa.com.',
            Name=Join('', ['pilosa{}.'.format(index), Ref(self.cluster_name), '.sandbox.pilosa.com.']),
            Type="A",
            TTL="300",
            ResourceRecords=[GetAtt("PilosaInstance{}".format(index), "PublicIp")],
        )

    def agent_public_record_set(self, index):
        return route53.RecordSetType(
            'AgentPublicRecordSet{}'.format(index),
            HostedZoneName='sandbox.pilosa.com.',
            Name=Join('', ['agent{}.'.format(index), Ref(self.cluster_name), '.sandbox.pilosa.com.']),
            Type="A",
            TTL="300",
            ResourceRecords=[GetAtt("PilosaAgentInstance{}".format(index), "PublicIp")],
        )

    def private_record_set(self, index):
        return route53.RecordSetType(
            'PilosaPrivateRecordSet{}'.format(index),
            HostedZoneId=Ref(self.hosted_zone),
            Name=Join('', ['pilosa{}.'.format(index), Ref(self.cluster_name), '.sandbox.pilosa.com.']),
            Type="A",
            TTL="300",
            ResourceRecords=[GetAtt("PilosaInstance{}".format(index), "PrivateIp")],
        )

    def agent_private_record_set(self, index):
        return route53.RecordSetType(
            'AgentPrivateRecordSet{}'.format(index),
            HostedZoneId=Ref(self.hosted_zone),
            Name=Join('', ['agent{}.'.format(index), Ref(self.cluster_name), '.sandbox.pilosa.com.']),
            Type="A",
            TTL="300",
            ResourceRecords=[GetAtt("PilosaAgentInstance{}".format(index), "PrivateIp")],
        )

    def process(self):
        super(PilosaTemplate, self).process()
        for i in range(self.cluster_size):
            self.template.add_resource(self.instance(i))
            self.template.add_resource(self.public_record_set(i))
            self.template.add_resource(self.private_record_set(i))
        for i in range(self.num_agents):
            self.template.add_resource(self.agent_instance(i))
            self.template.add_resource(self.agent_public_record_set(i))
            self.template.add_resource(self.agent_private_record_set(i))

def main():
    cluster_size = 3
    if len(sys.argv) > 1:
        cluster_size = int(sys.argv[1])
    num_agents = 1
    if len(sys.argv) > 2:
        num_agents = int(sys.argv[2])
    print(PilosaTemplate(cluster_size=cluster_size, num_agents=num_agents).output)

if __name__ == '__main__':
    main()
