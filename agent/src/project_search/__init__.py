"""Project search: the Search_Tool and its token handling (media-harness-projects-access Component 7).

The Search_Gateway is deliberately NOT a `GatewayConnection`: it is never registered with
`MCPManager`, so none of the user-token, delegation-grant, or machine-token paths can put another
credential on a search call. Each call carries exactly the turn's Project_Token.
"""
