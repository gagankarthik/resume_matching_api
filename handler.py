from mangum import Mangum

from main import app

# AWS Lambda entrypoint — set the Lambda handler to "handler.handler".
# Deploy behind a Lambda Function URL (not API Gateway) so /ingest can wait on
# the extraction Lambda (30–90s) without hitting the 29s API Gateway timeout.
# Set the Lambda timeout to 300s in the function configuration.
handler = Mangum(app, lifespan="off")
