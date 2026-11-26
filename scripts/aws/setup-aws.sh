#!/usr/bin/env bash
# One-time AWS setup for emike: IAM user, invoke permission, and a model-access
# sanity check for Amazon Nova Sonic.
#
# Prerequisites:
#   - aws CLI v2, authenticated as an admin-ish principal (aws sso login or
#     an existing IAM identity with iam:CreateUser/CreatePolicy/... rights)
#
# Usage:
#   bash scripts/aws/setup-aws.sh [region]
#
# Region defaults to us-east-1. The script never prints the secret key to the
# terminal beyond the final block you must copy into .env; if you redirect
# output, redirect it somewhere private.
set -euo pipefail

REGION="${1:-${AWS_REGION:-us-east-1}}"
USER_NAME="emike-assistant"
POLICY_NAME="EmikeNovaSonicInvoke"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POLICY_FILE="$SCRIPT_DIR/emike-sonic-policy.json"

command -v aws >/dev/null 2>&1 || { echo "aws CLI not found. Install AWS CLI v2 first."; exit 1; }

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Account: $ACCOUNT_ID / region: $REGION"

if ! aws iam get-user --user-name "$USER_NAME" >/dev/null 2>&1; then
  echo "Creating IAM user $USER_NAME ..."
  aws iam create-user --user-name "$USER_NAME" >/dev/null
else
  echo "IAM user $USER_NAME already exists"
fi

echo "Creating/confirming policy $POLICY_NAME ..."
POLICY_ARN="$(aws iam list-policies --scope Local --query "Policies[?PolicyName=='$POLICY_NAME'].Arn" --output text)"
if [ -z "$POLICY_ARN" ]; then
  POLICY_ARN="$(aws iam create-policy \
    --policy-name "$POLICY_NAME" \
    --policy-document "file://$POLICY_FILE" \
    --query Policy.Arn --output text)"
  echo "Created policy $POLICY_ARN"
else
  echo "Policy exists: $POLICY_ARN"
fi

aws iam attach-user-policy --user-name "$USER_NAME" --policy-arn "$POLICY_ARN"

# The generated policy file above uses us-east-1 ARNs by default; if another
# region is in use, pin the resource ARNs to it in emike-sonic-policy.json.

echo "Verifying Nova Sonic model availability in $REGION (requires Bedrock model access) ..."
for MODEL_ID in "amazon.nova-2-5-sonic" "amazon.nova-2-sonic-v1:0"; do
  if aws bedrock list-foundation-models --region "$REGION" \
      --query "modelSummaries[?modelId=='$MODEL_ID'].modelId" \
      --output text | grep -q .; then
    echo "  available: $MODEL_ID"
  else
    echo "  NOT LISTED: $MODEL_ID -- enable model access for it in the Bedrock console:"
    echo "    https://console.aws.amazon.com/bedrock/home?region=$REGION#/modelaccess"
  fi
done

echo
echo "Creating an access key for $USER_NAME ..."
echo "Copy the AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY below into .env on the Pi."
aws iam create-access-key --user-name "$USER_NAME" \
  --query "AccessKey.[AccessKeyId, SecretAccessKey]" --output text \
  | awk 'NR==1{print "  AWS_ACCESS_KEY_ID=" $1} NR==1{print "  AWS_SECRET_ACCESS_KEY=" $2}'
echo
echo "Done. IAM permissions granted: bedrock:InvokeModelWithBidirectionalStream"
echo "(limited to the Nova Sonic foundation-model ARNs in $POLICY_FILE)."
