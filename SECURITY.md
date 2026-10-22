# Security Policy

## Reporting a Vulnerability

If you discover a potential security issue in this project, we ask that you
notify AWS/Amazon Security via our
[vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/)
or directly via email to [aws-security@amazon.com](mailto:aws-security@amazon.com).

Please do **not** create a public GitHub issue for security vulnerabilities.

## Notes for this project

Two flags in this repository are dangerous if enabled outside local development —
never set them in a deployed environment:

- `DEV_BYPASS_AUTH=true` (proxy side) removes JWT signature/issuer/audience
  validation.
- `VITE_DEV_BYPASS_AUTH=true` (UI side) exposes a button that mints arbitrary
  client-side JWTs.

Cognito **self-registration is disabled by default** (`selfSignUpEnabled: false`)
and must stay disabled unless an explicit exemption is filed. See the README's
security section for details.

## Known limitations

- **CloudFront → UI load balancer is HTTP inside AWS.** Viewers reach the app over HTTPS at
  CloudFront, but CloudFront forwards to the UI proxy's Application Load Balancer over plain HTTP
  (`OriginProtocolPolicy.HTTP_ONLY`); that hop travels on the AWS network, not the public internet,
  but it is not encrypted. The load balancer is internet-facing and locked to CloudFront twice: its
  security group admits only the AWS-managed CloudFront origin-facing prefix list, and the UI proxy
  and the Capability API refuse every request (except the load balancer's own `/health` check) that
  does not carry the secret `X-Origin-Verify` header the distribution adds (a Secrets Manager value;
  CloudFront overwrites a viewer-sent header of that name). The planned hardening is a
  **CloudFront VPC origin** on an internal load balancer, which removes the public listener and the
  plain-HTTP hop altogether. Until then, if your requirements call for encryption in transit on every
  hop, terminate TLS on the load balancer with your own certificate and switch the origin to HTTPS.
