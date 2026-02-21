# server.py
import os
import sys
import json
import logging
import re
from typing import Optional, Dict, Any
import httpx
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv
 
# ============================================================================
# BASE DIRECTORY (anchored to this script's location)
# ============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
 
# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(os.path.join(BASE_DIR, 'servicenow_mcp.log'), encoding='utf-8')
    ]
)
logger = logging.getLogger('servicenow-mcp')
 
# ============================================================================
# CONFIGURATION VALIDATION
# ============================================================================
load_dotenv(os.path.join(BASE_DIR, ".env"))
 
def validate_config() -> None:
    """Validate that all required environment variables are set."""
    required_vars = ["SN_INSTANCE", "SN_USERNAME", "SN_PASSWORD"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
 
    if missing_vars:
        error_msg = f"Missing required environment variables: {', '.join(missing_vars)}"
        logger.error(error_msg)
        raise ValueError(error_msg)
 
    # Validate instance URL format
    instance_url = os.getenv("SN_INSTANCE", "")
    if not instance_url.startswith("https://"):
        error_msg = f"SN_INSTANCE must start with 'https://'. Got: {instance_url}"
        logger.error(error_msg)
        raise ValueError(error_msg)
 
    logger.info("Configuration validation successful")
 
# Validate configuration on startup
try:
    validate_config()
except ValueError as e:
    logger.error(f"Configuration validation failed: {e}")
    sys.exit(1)
 
# Load configuration
SN_INSTANCE = os.getenv("SN_INSTANCE")
SN_USER = os.getenv("SN_USERNAME")
SN_PASS = os.getenv("SN_PASSWORD")
 
logger.info(f"ServiceNow MCP Server initializing for instance: {SN_INSTANCE}")
 
# ============================================================================
# SERVICENOW CLIENT
# ============================================================================
class ServiceNowClient:
    """ServiceNow API client with connection pooling, timeouts, and error handling."""
 
    def __init__(self, instance: str, username: str, password: str):
        self.instance = instance
        self.auth = (username, password)
        self.client = httpx.AsyncClient(
            timeout=30.0,  # 30-second timeout
            verify=True,   # SSL verification enabled
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10)
        )
        logger.info("ServiceNow client initialized with SSL verification and 30s timeout")
 
    async def _handle_request_errors(self, func, *args, **kwargs) -> Dict[str, Any]:
        """Generic error handler for HTTP requests to keep methods clean."""
        response = None
        try:
            response = await func(*args, **kwargs)
            response.raise_for_status()
 
            # Guard against empty responses
            if not response.text.strip():
                error_msg = "ServiceNow returned 200 OK but with an empty response body."
                logger.error(error_msg)
                return {"success": False, "error": error_msg, "error_type": "empty_response"}
 
            # Guard against non-JSON responses (e.g. PDI hibernation pages)
            content_type = response.headers.get("content-type", "")
            if "application/json" not in content_type:
                body_preview = response.text[:500]
                error_msg = (
                    f"Expected JSON response but got Content-Type: {content_type}. "
                    f"Body preview: {body_preview}"
                )
                logger.error(error_msg)
                if "Instance Hibernating" in response.text or "<html" in response.text.lower():
                    error_msg += " — Your ServiceNow PDI appears to be hibernating. Wake it up via a browser first, then retry."
                return {"success": False, "error": error_msg, "error_type": "unexpected_content"}
 
            data = response.json()
            return {
                "success": True,
                "data": data.get('result', data)
            }
        except httpx.TimeoutException as e:
            error_msg = f"Request timeout: {str(e)}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg, "error_type": "timeout"}
        except httpx.HTTPStatusError as e:
            error_msg = f"HTTP {e.response.status_code} error: {e.response.text}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg, "error_type": "http_error", "status_code": e.response.status_code}
        except httpx.RequestError as e:
            error_msg = f"Network error: {str(e)}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg, "error_type": "network_error"}
        except json.JSONDecodeError as e:
            body_preview = response.text[:500] if response else "N/A"
            error_msg = f"Invalid JSON response: {str(e)}. Body preview: {body_preview}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg, "error_type": "json_error"}
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {"success": False, "error": error_msg, "error_type": "unexpected_error"}
 
    async def query_table(self, table: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """GET list of records from a table."""
        url = f"{self.instance}/api/now/table/{table}"
        headers = {"Accept": "application/json"}
        logger.info(f"Querying table: {table}")
 
        result = await self._handle_request_errors(self.client.get, url, auth=self.auth, headers=headers, params=params)
        if result["success"]:
            # Handle cases where result['data'] is a dictionary instead of a list
            data_list = result["data"] if isinstance(result["data"], list) else [result["data"]]
            result["data"] = data_list
            result["count"] = len(data_list)
            logger.info(f"Successfully retrieved {result['count']} record(s) from {table}")
        return result
 
    async def insert_record(self, table: str, payload: dict) -> Dict[str, Any]:
        """POST a new record to a table."""
        # <--- ADDED: ?sysparm_input_display_value=true to force ServiceNow to interpret dates exactly as written (Local Time)
        url = f"{self.instance}/api/now/table/{table}?sysparm_input_display_value=true"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        logger.info(f"Inserting record into table: {table}")
        return await self._handle_request_errors(self.client.post, url, auth=self.auth, headers=headers, json=payload)
 
    async def modify_record(self, table: str, sys_id: str, payload: dict) -> Dict[str, Any]:
        """PATCH an existing record in a table."""
        # <--- ADDED: ?sysparm_input_display_value=true to force ServiceNow to interpret dates exactly as written (Local Time)
        url = f"{self.instance}/api/now/table/{table}/{sys_id}?sysparm_input_display_value=true"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        logger.info(f"Modifying record {sys_id} in table: {table}")
        return await self._handle_request_errors(self.client.patch, url, auth=self.auth, headers=headers, json=payload)
 
    async def close(self):
        """Close the HTTP client connection."""
        await self.client.aclose()
        logger.info("ServiceNow client connection closed")
 
# Initialize ServiceNow client
sn_client = ServiceNowClient(SN_INSTANCE, SN_USER, SN_PASS)
 
# ============================================================================
# MCP SERVER INITIALIZATION
# ============================================================================
mcp = FastMCP("ServiceNow-MCP")
 
# ============================================================================
# UNIVERSAL TOOLS
# ============================================================================
@mcp.tool()
async def query_records(table_name: str, query: str = "", limit: int = 10, fields: str = "") -> str:
    """
    Universally queries ANY ServiceNow table to get a list of records.
    For complex ITIL workflows, call `read_framework_instructions` first to load the applicable SOP before querying.
 
    Args:
        table_name: The system name of the table (e.g., 'incident', 'change_request', 'cmdb_ci')
        query: Optional encoded query string (e.g., 'active=true^priority=1')
        limit: Max number of records to return (default 10)
        fields: Comma-separated list of fields to return (e.g., 'number,short_description')
    """
    logger.info(f"Tool called: query_records on {table_name}")
 
    params = {
        "sysparm_limit": min(limit, 100), # Hard cap at 100 for safety
        "sysparm_display_value": "true"   # <--- ADDED: Forces the API to return Human-Readable (Local) dates instead of backend UTC
    }
    if query: params["sysparm_query"] = query
    if fields: params["sysparm_fields"] = fields
 
    result = await sn_client.query_table(table_name, params)
    return json.dumps(result, indent=2)
 
@mcp.tool()
async def get_single_record(table_name: str, record_number: str, query_field: str = "number", fields: str = "") -> str:
    """
    Fetches details of a SINGLE record from ANY table based on its number (or other field).
 
    Args:
        table_name: The system name of the table (e.g., 'change_request')
        record_number: The display number (e.g., 'CHG0030001') or search value
        query_field: The field to search against (defaults to 'number')
        fields: Comma-separated list of fields to return
    """
    logger.info(f"Tool called: get_single_record for {record_number} in {table_name}")
 
    params = {
        "sysparm_query": f"{query_field}={record_number}",
        "sysparm_limit": 1,
        "sysparm_display_value": "true"   # <--- ADDED: Forces the API to return Human-Readable (Local) dates instead of backend UTC
    }
    if fields: params["sysparm_fields"] = fields
 
    result = await sn_client.query_table(table_name, params)
 
    if result["success"] and result.get("count", 0) == 0:
        return json.dumps({"success": False, "error": f"Record {record_number} not found in {table_name}"})
 
    return json.dumps(result, indent=2)
 
@mcp.tool()
async def create_record(table_name: str, payload_json: str) -> str:
    """
    Creates a new record in ANY ServiceNow table.
    Before creating records for ITIL processes (change, incident, etc.), always call
    `read_framework_instructions` first to load the applicable SOP.
 
    Args:
        table_name: The system name of the table (e.g., 'incident')
        payload_json: A valid JSON string containing the key-value pairs for the new record.
    """
    logger.info(f"Tool called: create_record in {table_name}")
    try:
        payload = json.loads(payload_json)
        result = await sn_client.insert_record(table_name, payload)
        return json.dumps(result, indent=2)
    except json.JSONDecodeError:
        return json.dumps({"success": False, "error": "Invalid JSON string provided for payload."})
 
@mcp.tool()
async def update_record(table_name: str, sys_id: str, payload_json: str) -> str:
    """
    Updates an existing record in ANY ServiceNow table using its sys_id.
    Before updating records for ITIL processes, always call `read_framework_instructions`
    first to load the applicable SOP.
 
    Args:
        table_name: The system name of the table.
        sys_id: The unique 32-character hex sys_id of the record to update.
        payload_json: A valid JSON string containing the fields to update.
    """
    logger.info(f"Tool called: update_record for {sys_id} in {table_name}")
 
    if not re.fullmatch(r'[0-9a-f]{32}', sys_id):
        return json.dumps({"success": False, "error": f"Invalid sys_id format: '{sys_id}'. Must be a 32-character hex string."})
 
    try:
        payload = json.loads(payload_json)
        result = await sn_client.modify_record(table_name, sys_id, payload)
        return json.dumps(result, indent=2)
    except json.JSONDecodeError:
        return json.dumps({"success": False, "error": "Invalid JSON string provided for payload."})
 
# NOTE: A delete_record tool is deliberately omitted to prevent accidental
# or destructive deletion of ServiceNow records via the AI agent. Deletions
# should be performed through the ServiceNow UI with proper authorization.
 
# ============================================================================
# DISCOVERABILITY TOOL
# ============================================================================
@mcp.tool()
def list_frameworks() -> str:
    """
    Lists all available framework documents organised by domain.
    Call this tool FIRST when the user mentions any ITIL domain (change, incident, problem,
    service request, CMDB, etc.) to discover which SOPs are available before taking action.
 
    Returns a JSON object mapping each domain to its available frameworks,
    so you know exactly what values to pass to the `read_framework_instructions` tool.
    """
    logger.info("Tool called: list_frameworks")
    docs_dir = os.path.join(BASE_DIR, "docs")
    result: Dict[str, list] = {}
 
    if not os.path.isdir(docs_dir):
        return json.dumps({"success": False, "error": "docs/ directory not found."})
 
    for domain in sorted(os.listdir(docs_dir)):
        domain_path = os.path.join(docs_dir, domain)
        if not os.path.isdir(domain_path):
            continue
        frameworks = sorted(
            f[:-3] for f in os.listdir(domain_path)
            if f.endswith(".md") and f != "_standards.md" and os.path.isfile(os.path.join(domain_path, f))
        )
        if frameworks:
            result[domain] = frameworks
 
    return json.dumps({"success": True, "frameworks": result}, indent=2)
 
# ============================================================================
# AGENTIC ROUTER (FRAMEWORK INGESTION TOOL)
# ============================================================================
@mcp.tool()
def read_framework_instructions(domain: str, framework: str, target: str = "") -> str:
    """
    Universal router for all standard operating procedures.
    The AI must call this tool to read the rules before executing any complex workflow.
 
    Args:
        domain: The folder name (e.g., 'change', 'incident', 'deal_tracker')
        framework: The markdown file name without .md (e.g., 'draft_normal_change')
        target: Optional. The specific record number to act on (e.g., 'CHG001', 'INC099')
    """
    logger.info(f"Tool called: read_framework_instructions for {domain}/{framework}")
 
    # --- Input sanitization ---
    _SAFE_NAME = re.compile(r'^[a-zA-Z0-9_-]+$')
    if not _SAFE_NAME.match(domain):
        return f"Error: Invalid domain name '{domain}'."
    if not _SAFE_NAME.match(framework):
        return f"Error: Invalid framework name '{framework}'."
 
    # 1. Fetch the Task Framework (The Workflow)
    # ------------------------------------------------------------------------
    file_path = os.path.join(BASE_DIR, "docs", domain, f"{framework}.md")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            instructions = f.read()
    except FileNotFoundError:
        return f"Error: No framework found at 'docs/{domain}/{framework}.md'. Use `list_frameworks` to see options."
 
    # 2. Automatically fetch Domain Standards (The Guardrails)
    # ------------------------------------------------------------------------
    standards_path = os.path.join(BASE_DIR, "docs", domain, "_standards.md")
    standards_text = ""
   
    if os.path.exists(standards_path):
        try:
            with open(standards_path, "r", encoding="utf-8") as f:
                standards_text = f"### GLOBAL {domain.upper()} STANDARDS\n{f.read()}\n\n---\n"
                logger.info(f"Injected standards from {domain}/_standards.md")
        except Exception as e:
            logger.warning(f"Failed to load standards file: {e}")
 
    # 3. Construct the Agentic Instruction Set
    # ------------------------------------------------------------------------
    prompt = f"""
    You are executing the '{framework}' framework for the '{domain}' domain.
 
    {standards_text}
    ### YOUR TASK INSTRUCTIONS
    {instructions}
 
    ---
    ### YOUR NEXT STEPS
    """
 
    if target:
        prompt += f"""
        1. You have been asked to review/process the record: **{target}**.
        2. Use your `get_single_record` or `query_records` tool to fetch the necessary data from ServiceNow.
        3. Apply the instructions strictly to the data you retrieved and output the final result.
        """
    else:
        prompt += """
        1. The user wants to initiate this framework but did not provide a specific target record.
        2. Interview the user to gather the required context or data points needed to fulfill the instructions.
        3. If the instructions require creating or updating a record, use your `create_record` or `update_record` tools once you have gathered enough information.
        """
 
    return prompt
 
# ============================================================================
# SERVER STARTUP
# ============================================================================
if __name__ == "__main__":
    logger.info("=" * 70)
    logger.info("ServiceNow MCP Server Starting (Universal Table API Edition)")
    logger.info(f"Instance: {SN_INSTANCE}")
    logger.info(f"User: {SN_USER[:2]}***" if SN_USER and len(SN_USER) > 2 else "User: ***")
    logger.info("=" * 70)
 
    try:
        mcp.run()
    except KeyboardInterrupt:
        logger.info("Server shutdown requested")
    except Exception as e:
        logger.error(f"Server error: {str(e)}", exc_info=True)
    finally:
        import asyncio
        asyncio.run(sn_client.close())
        logger.info("ServiceNow MCP Server stopped")

 