import streamlit as st
import subprocess
import os
import re
import openai
import requests
import zipfile
import platform
import stat
from pathlib import Path
import time # Added for exponential backoff placeholder

# --- Constants and Configuration ---
TERRAFORM_VERSION = "1.8.5"
# Note: OpenAI API key is pulled from st.secrets

# --- Content Sanitization Function ---
def sanitize_text(text: str) -> str:
    """
    Replaces common Unicode smart quotes and dashes with ASCII equivalents,
    then aggressively strips any remaining non-ASCII characters to ensure
    compatibility during API communication.
    """
    if not isinstance(text, str):
        return ""
    
    # Explicitly replace common smart quotes and dashes (to resolve \u201c error)
    text = text.replace('“', '"').replace('”', '"')
    text = text.replace('‘', "'").replace('’', "'")
    text = text.replace('—', '--').replace('–', '-')
    
    # Aggressively strip all remaining non-ASCII characters
    return text.encode('ascii', 'ignore').decode('ascii')


# --- Helper Function to Setup Terraform ---
@st.cache_resource
def get_terraform_executable(version=TERRAFORM_VERSION):
    """Downloads and prepares a specific version of Terraform."""
    terraform_dir = Path(f"./terraform_{version}").resolve()
    terraform_exe = terraform_dir / "terraform"

    if not terraform_exe.is_file():
        system = platform.system().lower()
        arch = platform.machine().lower()

        if arch == "x86_64":
            arch = "amd64"
        elif arch == "aarch64":
            arch = "arm64"

        # FIX: Removed markdown formatting from the URL string
        url = f"https://releases.hashicorp.com/terraform/{version}/terraform_{version}_{system}_{arch}.zip"
        
        response = requests.get(url, stream=True)
        response.raise_for_status()
        
        terraform_dir.mkdir(exist_ok=True)
        zip_path = terraform_dir / "terraform.zip"
        
        with open(zip_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(terraform_dir)
        
        zip_path.unlink() 
        
        # Make the terraform binary executable
        st_mode = terraform_exe.stat().st_mode
        terraform_exe.chmod(st_mode | stat.S_IEXEC)
            
    return str(terraform_exe)

# --- Initialize and Setup ---
st.set_page_config(layout="wide")
st.title("☁️ Terraform Code Assistant")
st.write("Describe your cloud infrastructure for AWS, Azure, or Google Cloud, and the AI will generate, validate, and correct the Terraform code for you.")

# Ensure Terraform is ready
try:
    terraform_executable_path = get_terraform_executable()
    if 'terraform_toast_shown' not in st.session_state:
        st.toast("✅ Terraform is ready!")
        st.session_state.terraform_toast_shown = True
except Exception as e:
    st.error(f"Failed to set up Terraform: {e}")
    terraform_executable_path = None

# Check for API key
openai_api_key = None
try:
    openai_api_key = st.secrets["OPENAI_API_KEY"]
except (FileNotFoundError, KeyError):
    pass # Handled in sidebar

# --- Initialize Session State ---
if 'terraform_code' not in st.session_state:
    st.session_state.terraform_code = '# Click "Generate with AI" to start.'
if 'validation_result' not in st.session_state:
    st.session_state.validation_result = ""
if 'has_errors' not in st.session_state:
    st.session_state.has_errors = False
if 'validated' not in st.session_state:
    st.session_state.validated = False
if 'raw_api_dump' not in st.session_state:
    st.session_state.raw_api_dump = ""

# --- Sidebar ---
with st.sidebar:
    st.header("Configuration")
    cloud_provider = st.selectbox("Select Cloud Provider", ["AWS", "Azure", "Google"])
    
    if openai_api_key:
        st.success("OpenAI API key loaded from secrets!")
    else:
        st.error("OpenAI API key not found!")
        st.info("Please add your OpenAI API key to your Streamlit app's secrets.")

    st.markdown("---")
    st.subheader("How to use:")
    st.markdown("""
    1. Select your cloud provider.
    2. Describe the resources you want to create in the text box.
    3. Click **Generate with AI**.
    4. Click **Validate** to check the code syntax.
    5. If errors exist, click **Correct with AI**.
    """)

# --- Main App Layout ---
col_editor, col_results = st.columns(2)

with col_editor:
    st.header("Your Infrastructure Request")
    
    example_prompts = {
        "AWS": "An S3 bucket for logging and a t3.small EC2 instance",
        "Azure": "An Azure Storage Account and a Standard_B1s virtual machine",
        "Google": "A Google Cloud Storage bucket and an e2-micro compute engine instance"
    }
    user_prompt = st.text_input("Describe the resources you want to create:", example_prompts[cloud_provider])

    st.header("Terraform Code")
    st.session_state.terraform_code = st.text_area(
        "Terraform HCL Code:",
        value=st.session_state.terraform_code,
        height=500,
        key="code_editor"
    )

# --- Utility Function for Code Extraction ---
def extract_code_content(response_content: str) -> str:
    """Extracts code content using a forgiving regex and aggressively cleans whitespace."""
    
    # CRITICAL FIX: Replace all non-standard whitespace (like \u00a0) with standard space
    # in the ENTIRE response before attempting regex matching. This prevents malformed 
    # delimiters like ` ```\u00a0hcl\n` from failing the match.
    response_content = re.sub(r'[^\S\r\n\t]+', ' ', response_content)
    
    # Now attempt the regex match on the cleaned content
    code_match = re.search(r'```[a-zA-Z]*\n(.*?)\n```', response_content, re.DOTALL)
    
    if code_match:
        content = code_match.group(1)
    else:
        # Fallback: Strip fences and use raw content
        content = response_content.strip()
        content = re.sub(r'^```[a-zA-Z]*\s*', '', content)
        content = re.sub(r'```$', '', content)
        
        if content != response_content.strip():
            # Only show warning if cleaning actually happened
            st.warning("⚠️ The AI response was not properly code-fenced. Using raw output.")
            
    return content.strip()


# --- Action Buttons ---
btn_col1, btn_col2, btn_col3 = st.columns(3)

with btn_col1:
    if st.button("🚀 Generate with AI", use_container_width=True, type="primary"):
        if not openai_api_key:
            st.error("Cannot generate code. Please configure your OpenAI API key in the app secrets.")
        elif not user_prompt:
            st.warning("Please describe the infrastructure you want to generate.")
        else:
            with st.spinner(f"AI is generating Terraform code for {cloud_provider}..."):
                clean_prompt = sanitize_text(user_prompt)
                response_content = ""
                st.session_state.raw_api_dump = "Attempting API call..." # Set pre-call status
                try:
                    client = openai.OpenAI(api_key=openai_api_key)
                    system_prompt = f"""
                    You are a Terraform code generation expert for {cloud_provider}.
                    Generate a complete, valid, and secure Terraform HCL configuration based on the user's request.
                    The configuration must be a single block of HCL code.
                    Do not include any explanations, markdown, or text outside of the code block.
                    Use appropriate resource names and tags.
                    - For AWS, default to 'us-east-1' region.
                    - For Azure, include a resource group.
                    - For Google, include a project and default to 'us-central1' region.
                    """
                    
                    # Use a basic retry mechanism (Exponential Backoff placeholder)
                    for i in range(3):
                        try:
                            completion = client.chat.completions.create(
                                model="gpt-4o", 
                                messages=[
                                    {"role": "system", "content": system_prompt}, 
                                    {"role": "user", "content": clean_prompt}
                                ]
                            )
                            response_content = completion.choices[0].message.content
                            st.session_state.raw_api_dump = f"RAW CONTENT RECEIVED (Length: {len(response_content)}):\n{response_content}"
                            break # Success, exit retry loop
                        except Exception as e:
                            if i < 2:
                                time.sleep(2 ** i) # Wait 1s, then 2s
                            else:
                                raise e # Re-raise error on final attempt

                    if response_content:
                        st.session_state.terraform_code = extract_code_content(response_content)
                    else:
                        st.error("The AI returned an empty response. Please check the Debug Panel for details.")
                        st.session_state.terraform_code = "# AI returned no content."
                        
                    st.session_state.validation_result, st.session_state.has_errors, st.session_state.validated = "", False, False
                    
                except openai.AuthenticationError:
                    st.error("Authentication Error: The OpenAI API key is invalid or has expired.")
                    st.session_state.raw_api_dump = "Authentication failed. Check your API key."
                except Exception as e:
                    st.error(f"An error occurred while communicating with OpenAI. This may be due to an environment/encoding issue: {e}")
                    st.session_state.raw_api_dump = f"ERROR DUMP:\n{e}\n\nSanitized Prompt Sent:\n{clean_prompt}"
            st.rerun()

with btn_col2:
    validate_disabled = not st.session_state.terraform_code or st.session_state.terraform_code.startswith('#')
    if st.button("✅ Validate", disabled=validate_disabled, use_container_width=True):
        if not terraform_executable_path or not Path(terraform_executable_path).is_file():
            st.error("Terraform executable is not available. Check installation logs.")
        else:
            st.session_state.validated = True
            temp_dir = "terraform_project"
            os.makedirs(temp_dir, exist_ok=True)
            
            with open(os.path.join(temp_dir, "main.tf"), "w") as f:
                f.write(st.session_state.terraform_code)
            
            with st.spinner("Running `terraform init` and `validate`..."):
                init_process = subprocess.run([terraform_executable_path, "init", "-no-color", "-upgrade"], cwd=temp_dir, capture_output=True, text=True)
                
                if init_process.returncode != 0:
                    st.session_state.validation_result = f"An error occurred during `terraform init`:\n{init_process.stderr}"
                    st.session_state.has_errors = True
                else:
                    validate_process = subprocess.run([terraform_executable_path, "validate", "-no-color"], cwd=temp_dir, capture_output=True, text=True)
                    
                    if validate_process.returncode == 0:
                        st.session_state.validation_result = "✅ Validation Successful: The configuration is valid."
                        st.session_state.has_errors = False
                    else:
                        st.session_state.validation_result = validate_process.stderr
                        st.session_state.has_errors = True

with btn_col3:
    correct_errors_disabled = not (st.session_state.validated and st.session_state.has_errors)
    if st.button("🛠️ Correct with AI", disabled=correct_errors_disabled, use_container_width=True):
        if not openai_api_key:
            st.error("Cannot correct code. Please configure your OpenAI API key in the app secrets.")
        else:
            with st.spinner("AI is attempting to correct the code..."):
                clean_code = sanitize_text(st.session_state.terraform_code)
                clean_result = sanitize_text(st.session_state.validation_result)

                try:
                    client = openai.OpenAI(api_key=openai_api_key)
                    system_prompt = "You are a Terraform code correction expert. The user will provide HCL code and a validation error. Fix the code to resolve the error. Only return the complete, corrected HCL code block without explanations."
                    
                    correction_prompt = f"""**Terraform Code with Errors:**
```hcl
{clean_code}
```

**Validation Error:**
```
{clean_result}
```
Please provide the corrected code."""
                    
                    completion = client.chat.completions.create(model="gpt-4o", messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": correction_prompt}])
                    
                    response_content = completion.choices[0].message.content if completion.choices and completion.choices[0].message else ""
                    
                    if response_content:
                        st.session_state.terraform_code = extract_code_content(response_content)
                        st.session_state.validation_result = "🔧 AI has attempted to correct the code. Please validate again."
                    else:
                        st.error("The AI returned an empty response during correction.")

                    st.session_state.has_errors, st.session_state.validated = False, False
                
                except openai.AuthenticationError:
                    st.error("Authentication Error: The OpenAI API key is invalid or has expired.")
                except Exception as e:
                    st.error(f"An error occurred during correction: {e}")
            st.rerun()

# --- Display Results Area ---
with col_results:
    st.header("Results")
    
    with st.expander("🛠️ DEBUG: Raw API Response"):
        if st.session_state.raw_api_dump:
            st.code(st.session_state.raw_api_dump, language="text")
        else:
            st.info("The raw API response will appear here after clicking 'Generate with AI'.")

    if st.session_state.validated:
        if st.session_state.has_errors:
            st.error("Validation Failed!")
            st.write("`<validation_result>`")
            st.code(st.session_state.validation_result, language="bash")
            st.write("`</validation_result>`")
        else:
            st.success("Validation Successful!")
            st.write("`<validation_result>`")
            st.code(st.session_state.validation_result, language="bash")
            st.write("`</validation_result>`")
    else:
        st.info("Generate and validate code to see the results here.")
