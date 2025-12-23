from fastapi import Request
from jose import jwt
import requests

# AUTHENTIK_JWKS_URL = "https://auth.yourdomain.com/application/o/google-mcp/jwks/"
# AUTHENTIK_ISSUER = "https://auth.yourdomain.com/application/o/google-mcp/"
# AUTHENTIK_AUDIENCE = "google-mcp"
AUTHENTIK_JWKS_URL = "http://localhost:9000/application/o/google-mcp/jwks/"
AUTHENTIK_ISSUER = "http://localhost:9000/application/o/google-mcp/"
AUTHENTIK_AUDIENCE = "google-mcp"

_jwks = requests.get(AUTHENTIK_JWKS_URL).json()

class AuthentikUser:
    def __init__(self, claims: dict):
        self.claims = claims
        self.email = claims.get("email")
        self.sub = claims.get("sub")
        self.groups = claims.get("groups", [])

def verify_authentik_request(request: Request) -> AuthentikUser:
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        raise Exception("Missing Authorization header")

    token = auth.split(" ")[1]

    claims = jwt.decode(
        token,
        _jwks,
        audience=AUTHENTIK_AUDIENCE,
        issuer=AUTHENTIK_ISSUER,
        algorithms=["RS256"]
    )

    return AuthentikUser(claims)









# Invoke-WebRequest `
#   -Uri "http://localhost:9000/application/o/token/" `
#   -Method POST `
#   -Headers @{ "Content-Type" = "application/x-www-form-urlencoded" } `
#   -Body @{
#     grant_type    = "password"
#     client_id     = "RgrmfNCxrvAnjxUZ1UkwuQ5edTcFkyro4wqnIVVC"
#     client_secret ="kDVMpFjgLwwNz9AJsjNkQt6kU6MmdFsetwAxmO7N8q1XeleBrFEcgpLp5fCXeC6OonDYk5lJvkZpjSAMSDC6rogJuRARZ8R1jY9lutN3m9x1qfoqrt3NaQyPknHxLa3R
# "
#     username      = "akadmin"
#     password      = "1234567890"
#     scope         = "openid email profile"
#   }
