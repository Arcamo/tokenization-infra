# Requirements Document

## Introduction

This document specifies spec 1 of the Infrastructure as Code (IaC) for the "Tokenización Inmobiliaria: arquitectura en AWS (MVP)" architecture. Spec 1 covers the minimal infrastructure needed to run the backend services and one front end. The IaC is written in AWS CDK v2 (Python) and provisions networking (VPC, private subnets, NAT gateways for egress to the Stellar network, the Stellar Anchor and KYC providers, VPC endpoints), encryption keys and secrets (KMS, Secrets Manager), identity (Cognito) with the sign-in and API settings for one front end (the investor/seller portal, hosted on an Amplify Hosting app created by hand), the API layer (API Gateway), compute (.NET Lambda domains and a KMS signer Lambda in private subnets), data stores (RDS PostgreSQL behind RDS Proxy, DynamoDB, S3), orchestration and messaging (Step Functions, EventBridge, EventBridge Scheduler, SQS), and the minimal observability needed to operate (log retention, X-Ray, DLQ alarms). CI/CD runs on GitHub Actions.

The architecture is split into several specs. Later specs (for example the planned `tokenization-agentcore-iac` spec) build on the resources of spec 1 through published extension parameters. Items removed from this spec are listed in the section "Deferred to Later Specs".

The repository contains infrastructure only. Lambda code packages are built in a separate application repository and referenced through configuration.

### Assumptions

- A1: The deployment targets a single AWS account. Only the `dev` stage is defined initially; the configuration model supports adding further stages (for example `staging`, `prod`) without code changes to stack logic.
- A2: The AWS region is configurable per stage, with a default of `us-east-1`. The earlier restriction to `us-east-1` came from the CloudFront-scoped WAF for Amplify; WAF is deferred, so spec 1 supports any region where the provisioned services are available.
- A3: The NAT gateway count is configurable per stage, with a minimum of 1 and a maximum of 2 (one per Availability Zone). The default is 1 for `dev` (cost) and 2 is recommended for prod-like stages (availability). Lambda functions need NAT egress to reach the Stellar network, the Stellar Anchor and KYC providers, so a count of 0 is not supported.
- A4: Lambda runtime is .NET (managed `dotnet8` runtime or later supported version), and Lambda artifacts are zip packages stored in an S3 artifact bucket identified in configuration by bucket name and object key.
- A5: The Signing_Key uses the KMS key spec `ECC_NIST_EDWARDS25519` (Ed25519), which matches the Stellar account key type.
- A6: The IaC avoids CloudFormation custom resources. CDK feature flags and construct options that add custom resources (for example `@aws-cdk/aws-ec2:restrictDefaultSecurityGroup`, Lambda `log_retention`, S3 `auto_delete_objects`) stay disabled unless a documented decision justifies them.
- A7: Application-level behavior (business logic, Stellar transaction construction, KYC provider integration code, front-end code) is out of scope.
- A8: Amplify Hosting apps (the Portal_App now, and the Stellar agent UI and backoffice apps later) are created and configured by hand, outside the IaC_App. This covers the app, branch, SPA rewrite rule, repository connection, custom domain and environment variables. The IaC_App creates no Amplify resources. It reads the Portal_Origin from Stage_Config and publishes the values the front end needs as Extension_Parameters.

## Glossary

- **IaC_App**: The AWS CDK Python application in this repository that defines all stacks.
- **Stage_Config**: The typed, per-stage configuration object (account, region, Availability Zones, NAT gateway count, Artifact_References, domain settings, sizing, alarm recipients) consumed by IaC_App.
- **Stage**: A named deployment environment (initially `dev`) described by one Stage_Config entry.
- **Network_Stack**: The CDK stack that defines the VPC, subnets, NAT gateways, security groups, VPC endpoints and flow logs.
- **Observability_Stack**: The CDK stack that defines the Alert_Topic and the DLQ alarms.
- **Bootstrap_Stack**: The CDK stack, defined in a separate CDK app and deployed separately, that defines the GitHub OIDC identity provider, the Deploy_Role and the Diff_Role.
- **Private_Subnet**: A VPC subnet with egress only through a NAT gateway and no direct inbound internet route.
- **Isolated_Subnet**: A VPC subnet with no route to the internet, used for the Database.
- **Lambda_Security_Group**: The security group attached to every Domain_Lambda and the Signer_Lambda.
- **Portal_App**: The single Amplify Hosting single-page application for investors and sellers. It is created and configured by hand, outside the IaC_App (A8).
- **Portal_Origin**: The HTTPS origin of the Portal_App, defined in Stage_Config. It is the Amplify default branch domain (for example `https://main.d1abc2def3.amplifyapp.com`) or a custom domain attached to the Portal_App by hand.
- **User_Pool**: The Amazon Cognito user pool.
- **Portal_Client**: The User_Pool app client used by the Portal_App.
- **Resource_Server**: The User_Pool resource server that defines the custom OAuth scopes for the Api.
- **Api**: The Amazon API Gateway REST API that fronts the Domain_Lambdas.
- **Cognito_Authorizer**: The API Gateway authorizer that validates User_Pool access tokens and their scopes.
- **KYC_Webhook_Routes**: The Api routes that receive callbacks from KYC providers and do not use Cognito_Authorizer.
- **Domain_Lambda**: One of the five .NET Lambda functions: Collateral_Identity, Sagas_Payments, Reports, Stellar_KYC (Stellar signing/submission and KYC), Trading_Operations.
- **Signer_Lambda**: The dedicated Lambda function that signs Stellar transactions with the Signing_Key.
- **Signing_Key**: The asymmetric AWS KMS key with key spec `ECC_NIST_EDWARDS25519` used only for signing.
- **Data_Key**: The symmetric customer managed KMS key used to encrypt data at rest.
- **Artifact_Reference**: A Stage_Config entry that identifies a code artifact by S3 bucket, S3 key and optional object version.
- **Kyc_Secrets**: The Secrets Manager secrets holding KYC provider credentials and webhook signing secrets.
- **Database**: The Amazon RDS for PostgreSQL instance.
- **Db_Proxy**: The Amazon RDS Proxy in front of Database with IAM authentication required.
- **Idempotency_Table**: The DynamoDB table storing idempotency keys.
- **Saga_Table**: The DynamoDB table storing saga state and messaging records.
- **Documents_Bucket**: The S3 bucket storing property and KYC documents.
- **Access_Log_Bucket**: The S3 bucket receiving server access logs for the Documents_Bucket.
- **Saga_State_Machine**: The AWS Step Functions state machine that orchestrates the tokenization saga.
- **Event_Bus**: The custom Amazon EventBridge event bus.
- **Scheduler**: The EventBridge Scheduler schedule group and its execution role.
- **Work_Queue**: An Amazon SQS queue consumed by a Domain_Lambda.
- **DLQ**: An SQS dead-letter queue attached to a Work_Queue or asynchronous target.
- **Alert_Topic**: The Amazon SNS topic that receives alarm notifications.
- **Extension_Parameters**: The SSM Parameter Store parameters through which IaC_App publishes resource identifiers for later specs.
- **Pipeline**: The GitHub Actions workflows in this repository.
- **Deploy_Role**: The IAM role assumed by Pipeline deployment jobs through GitHub OIDC federation.
- **Diff_Role**: The read-only IAM role assumed by Pipeline pull request jobs through GitHub OIDC federation to run `cdk diff`.

## Requirements

### Requirement 1: CDK project and stage configuration

**User Story:** As a platform engineer, I want a typed, parameterized CDK Python project, so that I can deploy `dev` now and add stages later without changing stack code.

#### Acceptance Criteria

1. THE IaC_App SHALL be an AWS CDK v2 application written in Python 3.12 or later, with dependencies pinned in a uv lock file, type-checked by the Pipeline with mypy in strict mode or pyright, and linted by the Pipeline with ruff.
2. THE IaC_App SHALL read account, region, the explicit list of Availability Zones, NAT gateway count, Artifact_References, the Portal_Origin, sizing values and removal policy from Stage_Config.
3. THE IaC_App SHALL define a Stage_Config entry for the `dev` Stage.
4. WHEN a Stage name is supplied through the CDK context key `stage`, THE IaC_App SHALL synthesize the stacks for the matching Stage_Config entry.
5. IF the supplied Stage name has no matching Stage_Config entry, THEN THE IaC_App SHALL fail synthesis with an error message naming the unknown Stage.
6. IF a required Stage_Config field is missing or invalid, THEN THE IaC_App SHALL fail synthesis with an error message naming the field.
7. WHERE a Stage_Config entry omits the region, THE IaC_App SHALL use `us-east-1`.
8. WHERE a Stage_Config entry defines a region, THE IaC_App SHALL deploy every stack of that Stage to the defined region.
9. THE IaC_App SHALL include the Stage name as a prefix or path segment in every stack name and every name of a named resource, except SSM parameter names under `/{project}/{stage}/`, KMS alias names under `alias/`, log group names under `/aws/` paths or fixed by API Gateway (`API-Gateway-Execution-Logs_{restApiId}/{stage}`), User_Pool group names and the Resource_Server identifier.
10. THE IaC_App SHALL apply the tags `project`, `stage` and `managed-by=cdk` to every taggable resource.
11. THE IaC_App SHALL split resources into separate stacks for network, security and keys, data, messaging and orchestration, API and compute, identity, and observability, and SHALL define the Bootstrap_Stack of Requirement 13.5 in a separate CDK app.
12. THE IaC_App SHALL synthesize templates that contain no resources of type `AWS::CloudFormation::CustomResource` or `Custom::*`, except resources listed in a documented allowlist with a justification.
13. THE IaC_App SHALL keep the CDK feature flag `@aws-cdk/aws-ec2:restrictDefaultSecurityGroup` disabled.

### Requirement 2: Networking and egress

**User Story:** As a platform engineer, I want Lambda functions in private subnets with controlled egress, so that backend services reach the Stellar network, the Stellar Anchor and KYC providers without being exposed to inbound internet traffic.

#### Acceptance Criteria

1. THE Network_Stack SHALL create a VPC spanning the 2 Availability Zones listed in Stage_Config with public subnets, Private_Subnets and Isolated_Subnets in each Availability Zone.
2. THE Network_Stack SHALL create the number of NAT gateways specified in Stage_Config, with a default of 1 for the `dev` Stage.
3. IF the Stage_Config NAT gateway count is less than 1 or greater than 2, THEN THE IaC_App SHALL fail synthesis with an error message naming the NAT gateway count field.
4. THE Network_Stack SHALL route Private_Subnet internet-bound traffic through the NAT gateways.
5. THE Network_Stack SHALL create gateway VPC endpoints for S3 and DynamoDB.
6. THE Network_Stack SHALL create interface VPC endpoints for Secrets Manager, KMS, SQS, Step Functions, EventBridge, CloudWatch Logs, X-Ray and STS in the Private_Subnets.
7. THE Network_Stack SHALL create the Lambda_Security_Group with outbound rules limited to HTTPS (TCP 443) to any destination and TCP 5432 to the Db_Proxy security group.
8. THE Network_Stack SHALL create a Db_Proxy security group that permits inbound TCP 5432 only from the Lambda_Security_Group.
9. THE Network_Stack SHALL create a Database security group that permits inbound TCP 5432 only from the Db_Proxy security group.
10. THE Network_Stack SHALL create an interface endpoint security group that permits inbound TCP 443 only from the VPC CIDR range.
11. THE Network_Stack SHALL enable VPC flow logs for all traffic, delivered to a CloudWatch Logs log group with the retention period defined in Stage_Config.

### Requirement 3: Encryption keys and secrets

**User Story:** As a security engineer, I want dedicated KMS keys and managed secrets, so that data is encrypted with customer managed keys and Stellar signing keys never leave KMS.

#### Acceptance Criteria

1. THE IaC_App SHALL create the Data_Key as a symmetric customer managed KMS key with automatic key rotation enabled.
2. THE IaC_App SHALL create the Signing_Key as an asymmetric KMS key with key usage `SIGN_VERIFY` and key spec `ECC_NIST_EDWARDS25519`.
3. THE IaC_App SHALL grant `kms:Sign` on the Signing_Key only to the Signer_Lambda execution role.
4. THE IaC_App SHALL grant `kms:GetPublicKey` on the Signing_Key only to the Signer_Lambda execution role and the Stellar_KYC Domain_Lambda execution role.
5. THE IaC_App SHALL create Kyc_Secrets in Secrets Manager encrypted with the Data_Key, with secret values left for operators to populate after deployment.
6. THE IaC_App SHALL grant `secretsmanager:GetSecretValue` on Kyc_Secrets only to the Stellar_KYC Domain_Lambda execution role.
7. THE IaC_App SHALL apply a `RETAIN` removal policy to the Signing_Key and the Data_Key in every Stage.

### Requirement 4: Identity and front-end settings

**User Story:** As a product owner, I want investors and sellers to sign in through Cognito and use one portal hosted on an Amplify app created by hand, so that the backend services can be exercised end to end through a real front end.

#### Acceptance Criteria

1. THE IaC_App SHALL create the User_Pool with email sign-in, self sign-up enabled, a minimum password length of 12 characters, and MFA set to optional with TOTP.
2. THE IaC_App SHALL create a User_Pool group for each group name listed in Stage_Config, with a default list of `investor`, `seller` and `backoffice`.
3. THE IaC_App SHALL create the Resource_Server with the identifier and custom scopes defined in Stage_Config.
4. THE IaC_App SHALL create the Portal_Client as a public client without a client secret that allows only the authorization code grant, for use with PKCE by the Portal_App.
5. THE IaC_App SHALL configure the Portal_Client OAuth scopes as `openid`, `email`, `profile` and the Resource_Server scopes listed for the Portal_App in Stage_Config.
6. THE IaC_App SHALL configure the Portal_Client callback URLs and logout URLs as the Portal_Origin combined with the callback path and logout path defined in Stage_Config.
7. WHERE Stage_Config lists additional local development callback URLs, THE IaC_App SHALL add those URLs to the Portal_Client callback URLs, and add the origin of each of those URLs combined with the logout path defined in Stage_Config to the Portal_Client logout URLs.
8. THE IaC_App SHALL create a User_Pool hosted UI domain with the prefix defined in Stage_Config.
9. THE IaC_App SHALL read the Portal_Origin from Stage_Config.
10. IF the Portal_Origin in Stage_Config is not an `https` origin without a path, query or fragment, THEN THE IaC_App SHALL fail synthesis with an error message naming the field.
11. THE IaC_App SHALL create no Amplify Hosting resources (A8).

### Requirement 5: API layer

**User Story:** As a backend developer, I want an API Gateway with Cognito authorization and separate KYC webhook routes, so that the Portal_App calls protected endpoints and KYC providers deliver callbacks.

#### Acceptance Criteria

1. THE IaC_App SHALL create the Api as a regional API Gateway REST API.
2. THE IaC_App SHALL attach the Cognito_Authorizer, backed by the User_Pool, to every Api method except KYC_Webhook_Routes and CORS preflight `OPTIONS` methods.
3. THE IaC_App SHALL set the authorization scopes of each Api method protected by the Cognito_Authorizer to the Resource_Server scopes defined for that method's resource path in Stage_Config.
4. THE IaC_App SHALL create one Api resource path per Domain_Lambda with a Lambda proxy integration to that Domain_Lambda.
5. THE IaC_App SHALL create KYC_Webhook_Routes as `POST` methods under the path `/webhooks/kyc/{provider}` integrated with the Stellar_KYC Domain_Lambda.
6. THE IaC_App SHALL enable Api access logging to a CloudWatch Logs log group with the retention period defined in Stage_Config.
7. THE IaC_App SHALL enable X-Ray tracing on the Api stage.
8. THE IaC_App SHALL configure Api stage-level throttling with the rate limit and burst limit defined in Stage_Config.
9. THE IaC_App SHALL configure CORS on the Api to allow only the Portal_Origin and the local development origins listed in Stage_Config.
10. THE IaC_App SHALL add CORS headers allowing only the Portal_Origin to API Gateway `DEFAULT_4XX` and `DEFAULT_5XX` gateway responses.

### Requirement 6: Compute (Lambda domains and signer)

**User Story:** As a backend developer, I want the five .NET Lambda domains and the KMS signer Lambda provisioned from externally built artifacts, so that the application repository deploys code independently of the infrastructure.

#### Acceptance Criteria

1. THE IaC_App SHALL create the five Domain_Lambdas and the Signer_Lambda with the .NET runtime defined in Stage_Config.
2. THE IaC_App SHALL load the code for each Domain_Lambda and the Signer_Lambda from the Artifact_Reference defined for that function in Stage_Config.
3. THE IaC_App SHALL attach each Domain_Lambda and the Signer_Lambda to the Private_Subnets and the Lambda_Security_Group.
4. THE IaC_App SHALL enable X-Ray active tracing on each Domain_Lambda and the Signer_Lambda.
5. THE IaC_App SHALL set memory size, timeout and reserved concurrency for each function from Stage_Config.
6. THE IaC_App SHALL create a separate IAM execution role per function with permissions limited to the resources that function uses.
7. THE IaC_App SHALL grant `lambda:InvokeFunction` on the Signer_Lambda only to the Stellar_KYC Domain_Lambda execution role.
8. THE IaC_App SHALL pass to each function, through environment variables, the identifiers of the resources that function uses (for example table names, bucket names, queue URLs, Event_Bus name, Db_Proxy endpoint, secret ARNs, Signer_Lambda name).
9. THE IaC_App SHALL create an explicit CloudWatch Logs log group for each function, with the retention period defined in Stage_Config.

### Requirement 7: Relational database

**User Story:** As a backend developer, I want PostgreSQL behind RDS Proxy with IAM authentication, so that Lambda functions use pooled connections without static database passwords.

#### Acceptance Criteria

1. THE IaC_App SHALL create the Database as an RDS for PostgreSQL instance in the Isolated_Subnets, with storage encrypted by the Data_Key.
2. THE IaC_App SHALL set the Database engine version, instance class, allocated storage, Multi-AZ flag and backup retention from Stage_Config.
3. THE IaC_App SHALL store the Database master credentials in a Secrets Manager secret generated at deployment and encrypted with the Data_Key.
4. THE IaC_App SHALL create the Db_Proxy in the Private_Subnets with IAM authentication set to required and TLS required.
5. THE IaC_App SHALL grant `rds-db:connect` on the Db_Proxy only to the execution roles of the Domain_Lambdas listed as Database users in Stage_Config.
6. WHERE the Stage_Config removal policy is `RETAIN`, THE IaC_App SHALL enable deletion protection on the Database.

### Requirement 8: NoSQL tables and document storage

**User Story:** As a backend developer, I want DynamoDB tables and an S3 documents bucket, so that services store idempotency keys, saga state and documents.

#### Acceptance Criteria

1. THE IaC_App SHALL create the Idempotency_Table and the Saga_Table with on-demand capacity, encryption with the Data_Key and point-in-time recovery enabled.
2. THE IaC_App SHALL enable a time-to-live attribute named `expiresAt` on the Idempotency_Table.
3. THE IaC_App SHALL enable DynamoDB Streams with new and old images on the Saga_Table.
4. THE IaC_App SHALL create the Documents_Bucket with Block Public Access enabled for all four settings, versioning enabled, encryption with the Data_Key and a bucket policy that denies requests not using TLS.
5. THE IaC_App SHALL enable server access logging for the Documents_Bucket to the Access_Log_Bucket.
6. THE IaC_App SHALL create the Access_Log_Bucket with Block Public Access enabled for all four settings, S3-managed encryption, a bucket policy that denies requests not using TLS, and a lifecycle expiration period defined in Stage_Config.

### Requirement 9: Orchestration and messaging

**User Story:** As a backend developer, I want a Step Functions saga, an EventBridge bus, schedules and SQS queues with DLQs, so that multi-step tokenization flows run reliably and asynchronously.

#### Acceptance Criteria

1. THE IaC_App SHALL create the Saga_State_Machine as a Standard workflow with X-Ray tracing enabled and logging of all events to a CloudWatch Logs log group with the retention period defined in Stage_Config.
2. THE IaC_App SHALL load the Saga_State_Machine definition from a definition file referenced in Stage_Config, with Domain_Lambda ARNs substituted at synthesis.
3. THE IaC_App SHALL grant the Saga_State_Machine role `lambda:InvokeFunction` on the Domain_Lambdas referenced in the definition.
4. THE IaC_App SHALL grant `states:StartExecution` on the Saga_State_Machine to the Sagas_Payments Domain_Lambda execution role.
5. THE IaC_App SHALL create the Event_Bus with an event archive whose retention period is defined in Stage_Config.
6. THE IaC_App SHALL create a Work_Queue for each queue defined in Stage_Config, encrypted with the Data_Key, each with a DLQ and a `maxReceiveCount` defined in Stage_Config.
7. THE IaC_App SHALL create EventBridge rules from Stage_Config that route Event_Bus events to Work_Queues or the Saga_State_Machine, each target with a DLQ.
8. THE IaC_App SHALL create SQS event source mappings from each Work_Queue to the Domain_Lambda defined in Stage_Config, with partial batch failure reporting enabled.
9. THE IaC_App SHALL create the Scheduler schedule group and an execution role allowed to invoke only the targets listed in Stage_Config.
10. THE IaC_App SHALL grant `events:PutEvents` on the Event_Bus to every Domain_Lambda execution role.

### Requirement 10: Minimal observability

**User Story:** As an operations engineer, I want log retention, tracing and alarms on failed messages, so that I can troubleshoot the backend and learn about stuck work.

#### Acceptance Criteria

1. THE IaC_App SHALL set a retention period from Stage_Config on every CloudWatch Logs log group that IaC_App creates, including the API Gateway execution log group of the Api stage, which IaC_App creates before the Api stage is deployed.
2. THE Observability_Stack SHALL create the Alert_Topic encrypted with the Data_Key.
3. THE Observability_Stack SHALL subscribe each email address listed in Stage_Config to the Alert_Topic.
4. WHEN the approximate number of visible messages in any DLQ is greater than 0, THE Observability_Stack SHALL set a CloudWatch alarm to the `ALARM` state.
5. WHEN a DLQ alarm changes to the `ALARM` state or the `OK` state, THE Observability_Stack SHALL publish a notification to the Alert_Topic.

### Requirement 11: Extension points for later specs

**User Story:** As a platform engineer working on a later spec, I want the identifiers of shared spec 1 resources published in a stable location, so that later stacks can attach to the VPC, Api, User_Pool, keys and Event_Bus without modifying spec 1 stacks.

#### Acceptance Criteria

1. THE IaC_App SHALL publish Extension_Parameters under the path prefix `/{project}/{stage}/` with the project name defined in Stage_Config.
2. THE IaC_App SHALL publish Extension_Parameters for the VPC ID, the Private_Subnet IDs, the Isolated_Subnet IDs and the Lambda_Security_Group ID.
3. THE IaC_App SHALL publish Extension_Parameters for the Api ID, the Api root resource ID and the Api base URL.
4. THE IaC_App SHALL publish Extension_Parameters for the User_Pool ID, the User_Pool ARN, the Resource_Server identifier, the Portal_Client ID and the hosted UI domain, so that operators can set the Portal_App environment variables by hand.
5. THE IaC_App SHALL publish Extension_Parameters for the Data_Key ARN and the Signing_Key ARN.
6. THE IaC_App SHALL publish Extension_Parameters for the Event_Bus name and the Event_Bus ARN.
7. THE IaC_App SHALL publish Extension_Parameters for the Alert_Topic ARN.

### Requirement 12: Testing

**User Story:** As a platform engineer, I want automated tests on the synthesized templates, so that regressions in security and wiring are caught before deployment.

#### Acceptance Criteria

1. THE IaC_App SHALL include pytest unit tests using the `aws_cdk.assertions` module that verify the resources required by Requirements 2 through 11 exist in the synthesized templates.
2. THE IaC_App SHALL include tests that verify every S3 bucket blocks public access and every Domain_Lambda and the Signer_Lambda run in the Private_Subnets.
3. THE IaC_App SHALL include a test that verifies `kms:Sign` on the Signing_Key appears only in the Signer_Lambda execution role policy.
4. THE IaC_App SHALL include a test that verifies the synthesized templates contain no `AWS::Amplify::*` resources.
5. THE IaC_App SHALL include a test that verifies the synthesized templates contain no custom resources outside the documented allowlist.
6. THE IaC_App SHALL run `AwsSolutionsChecks` from the `cdk-nag` Python package during synthesis.
7. IF cdk-nag reports an error that has no documented suppression with a reason, THEN THE IaC_App SHALL fail synthesis.
8. THE IaC_App SHALL include tests that verify synthesis fails for an unknown Stage, for a missing required Stage_Config field and for a NAT gateway count of 0.

### Requirement 13: CI/CD with GitHub Actions

**User Story:** As a platform engineer, I want GitHub Actions to validate pull requests and deploy merges with short-lived credentials, so that infrastructure changes are reviewed and deployed without stored AWS keys.

#### Acceptance Criteria

1. WHEN a pull request targets the `main` branch, THE Pipeline SHALL run dependency installation from the lock file, linting, type checking, unit tests and `cdk synth` for the `dev` Stage.
2. WHEN a pull request targets the `main` branch, THE Pipeline SHALL publish the `cdk diff` output for the `dev` Stage as a pull request comment.
3. WHEN a commit is merged to the `main` branch, THE Pipeline SHALL deploy all stacks except the Bootstrap_Stack for the `dev` Stage.
4. THE Pipeline SHALL obtain AWS credentials through GitHub OIDC federation by assuming the Deploy_Role in deployment jobs and the Diff_Role in pull request jobs.
5. THE IaC_App SHALL define the GitHub OIDC identity provider, the Deploy_Role with a trust policy restricted to the repository and branch defined in Stage_Config, and the Diff_Role with a trust policy restricted to pull requests of the repository defined in Stage_Config and permission to assume only the CDK lookup role, in the Bootstrap_Stack.
6. IF any test, lint, type check or synthesis step fails, THEN THE Pipeline SHALL stop the workflow and mark the run as failed.
7. THE Pipeline SHALL use a GitHub environment named after the Stage for deployment jobs.

### Requirement 14: Documentation

**User Story:** As a new team member, I want documentation for setup and deployment, so that I can bootstrap, configure and deploy the infrastructure.

#### Acceptance Criteria

1. THE IaC_App SHALL include a README that describes prerequisites, CDK bootstrap, deployment of the Bootstrap_Stack, Stage_Config fields and the deploy commands.
2. THE IaC_App README SHALL list the Artifact_References that the application repository must publish before deployment.
3. THE IaC_App README SHALL list the post-deployment manual steps, including populating Kyc_Secrets values and confirming Alert_Topic email subscriptions.
4. THE IaC_App README SHALL list the Extension_Parameters with their names and meanings.
5. THE IaC_App README SHALL list the items in the section "Deferred to Later Specs" of this document.
6. THE IaC_App README SHALL describe the manual Portal_App setup: creating the Amplify app and branch before the first deployment, recording the Portal_Origin in Stage_Config, adding the SPA rewrite rule, and setting the environment variables from the Extension_Parameters after deployment.

## Deferred to Later Specs

The following items were part of the original full-architecture spec and are out of scope for spec 1. They are kept here so that nothing is lost.

### Spec 2 (planned): `tokenization-agentcore-iac`

- Bedrock AgentCore Runtime hosting the agent artifact.
- Bedrock AgentCore Memory.
- Bedrock AgentCore Gateway exposing Api operations as agent tools, with inbound authorization against the User_Pool.
- The AgentCore DynamoDB table.
- A Cognito machine-to-machine app client (client credentials grant) for the AgentCore Gateway.
- The Cognito settings for the Stellar agent UI and the `stellar-agent` User_Pool group. The Stellar agent UI Amplify Hosting app itself is created by hand (A8).

### Undecided or later

- AWS WAF web ACLs for the Api and for Amplify apps (managed rule groups and rate-based rule).
- Amazon GuardDuty detector.
- Analytics and audit pipeline: Kinesis data stream, Kinesis Data Firehose, analytics S3 bucket, Glue database and table, Athena workgroup, and the related Domain_Lambda and Reports grants.
- AWS CloudTrail trail and Documents_Bucket data events.
- CloudWatch dashboard and alarms beyond DLQ alarms (Lambda errors and throttles, Api 5XX, Saga_State_Machine failures, Database CPU).
- The Cognito and API settings for the backoffice front end. Its Amplify Hosting app is created by hand (A8).
- Cognito advanced security features (threat protection).
