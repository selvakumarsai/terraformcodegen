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

# --- App Title and Description ---
st.set_page_config(layout="wide")
st.title("☁️ Terraform Code Assistant")
st.write("Describe your cloud infrastructure for AWS, Azure, or Google Cloud, and the AI will generate, validate, and correct the Terraform code for you. This app is ready for deployment on Streamlit Community Cloud.")

# --- Helper Function to Setup Terraform ---
@st.cache_resource
def get_terraform_executable(version="1.8.5"):
    """
    Downloads and prepares a specific version of Terraform, returning an absolute path.
    This function's output is cached to avoid re-downloading.
    It does NOT contain any Streamlit UI calls.
    Returns the absolute path to the executable or raises an exception.
    """
    # Use .resolve() to ensure the path is absolute, which is more robust in cloud environments.
    terraform_dir = Path(f"./terraform_{version}").resolve()
    terraform_exe = terraform_dir / "terraform"

    if not terraform_exe.is_file():
        system = platform.system().lower()
        arch = platform.machine().lower()

        if arch == "x86_64":
            arch = "amd64"
        elif arch == "aarch64":
            arch = "arm64"

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
        
        zip_path.unlink() # Clean up the zip file
        
        # Make the terraform binary executable
        st_mode = terraform_exe.stat().st_mode
        terraform_exe.chmod(st_mode | stat.S_IEXEC)
            
    return str(terraform_exe)

# --- Main App Logic ---
# This wrapper function calls the cached function and handles UI messages.
def setup_terraform_and_show_status():
    """
    Ensures Terraform is set up and shows status messages to the user.
    """
    try:
        path = get_terraform_executable()
        # A one-time toast message can be shown if we use a session_state flag
        if 'terraform_toast_shown' not in st.session_state:
            st.toast("✅ Terraform is ready!")
            st.session_state.terraform_toast_shown = True
        return path
    except Exception as e:
        st.error(f"Failed to set up Terraform: {e}")
        return None

# For deployment, you will need a requirements.txt file with the following content:
# streamlit
# openai
# requests

terraform_executable_path = setup_terraform_and_show_status()

# --- Sidebar for Configuration ---
with st.sidebar:
    st.header("Configuration")
    cloud_provider = st.selectbox("Select Cloud Provider", ["AWS", "Azure", "Google"])
    
    # Check for API key in st.secrets. No manual fallback.
    openai_api_key = None
    try:
        openai_api_key = st.secrets["OPENAI_API_KEY"]
        st.success("OpenAI API key loaded from secrets!")
    except (FileNotFoundError, KeyError):
        st.error("OpenAI API key not found!")
        st.info("Please add your OpenAI API key to your Streamlit app's secrets. In your app dashboard, go to Settings > Secrets and add a key named `OPENAI_API_KEY`.")

    st.markdown("---")
    st.subheader("How to use:")
    st.markdown("""
    1.  Select your cloud provider.
    2.  Ensure your OpenAI API key is set in the app's secrets.
    3.  Describe the resources you want in the text box.
    4.  Click **Generate with AI**.
    5.  Click **Validate** to check the code.
    6.  If errors exist, click **Correct with AI**.
    """)

# --- Initialize Session State ---
if 'terraform_code' not in st.session_state:
    st.session_state.terraform_code = f'# Describe your {cloud_provider} infrastructure above and click "Generate with AI"'
if 'validation_result' not in st.session_state:
    st.session_state.validation_result = ""
if 'has_errors' not in st.session_state:
    st.session_state.has_errors = False
if 'validated' not in st.session_state:
    st.session_state.validated = False
if 'raw_response_debug' not in st.session_state: # NEW: Debug state for raw API response
    st.session_state.raw_response_debug = ""

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

# --- Action Buttons ---
btn_col1, btn_col2, btn_col3 = st.columns(3)

with btn_col1:
    if st.button("🚀 Generate with AI", use_container_width=True, type="primary"):
        st.session_state.raw_response_debug = "" # Clear debug flag before new run
        
        if not openai_api_key:
            st.error("Cannot generate code. Please configure your OpenAI API key in the app secrets.")
        elif not user_prompt:
            st.warning("Please describe the infrastructure you want to generate.")
        else:
            with st.spinner(f"AI is generating Terraform code for {cloud_provider}..."):
                try:
                    # --- FIX: Sanitize prompt to remove non-ASCII 'smart quotes' that cause encoding errors ---
                    sanitized_prompt = user_prompt.replace('“', '"').replace('”', '"').replace('’', "'").replace('—', '-')
                    
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
                    completion = client.chat.completions.create(
                        model="gpt-4o", 
                        messages=[
                            {"role": "system", "content": system_prompt}, 
                            {"role": "user", "content": sanitized_prompt} # Use sanitized prompt
                        ]
                    )
                    
                    if not (completion.choices and completion.choices[0].message and completion.choices[0].message.content):
                        st.error("API Error: Received an empty or malformed response from the OpenAI API.")
                        response_content = ""
                        st.session_state.raw_response_debug = "API returned an empty or malformed completion object."
                    else:
                        response_content = completion.choices[0].message.content
                        st.session_state.raw_response_debug = response_content # Store raw response for debug

                    # Use a more forgiving regex to capture the code block content, 
                    # regardless of the language specifier (e.g., hcl, terraform, or empty).
                    code_match = re.search(r'```[a-zA-Z]*\n(.*?)\n```', response_content, re.DOTALL)
                    
                    if code_match:
                        # If a code block is found, use its captured content.
                        st.session_state.terraform_code = code_match.group(1).strip()
                        st.session_state.validation_result, st.session_state.has_errors, st.session_state.validated = "", False, False
                        st.session_state.raw_response_debug = "" # Clear debug if extraction was successful
                    elif response_content:
                        # Fallback: If content exists but no code block was detected, try to strip fences
                        cleaned_content = response_content.strip()
                        cleaned_content = re.sub(r'^```[a-zA-Z]*\s*', '', cleaned_content)
                        cleaned_content = re.sub(r'```$', '', cleaned_content)
                        st.session_state.terraform_code = cleaned_content.strip()
                        st.warning("⚠️ The AI response did not contain a standard markdown code block. Using the raw output.")
                        st.session_state.validation_result, st.session_state.has_errors, st.session_state.validated = "", False, False
                    else:
                        st.session_state.terraform_code = "# AI returned no content."
                        st.session_state.validation_result = "Failed to generate code."
                        st.session_state.has_errors = True
                        st.session_state.validated = False

                except openai.AuthenticationError:
                    st.error("Authentication Error: The OpenAI API key is invalid or has expired.")
                    st.session_state.raw_response_debug = "Authentication failed. Check your API key."
                except Exception as e:
                    st.error(f"An error occurred while communicating with OpenAI: {e}")
                    st.session_state.raw_response_debug = f"General API communication error: {e}"
            st.rerun()

with btn_col2:
    validate_disabled = not st.session_state.terraform_code or st.session_state.terraform_code.startswith('#')
    if st.button("✅ Validate", disabled=validate_disabled, use_container_width=True):
        if not terraform_executable_path or not Path(terraform_executable_path).is_file():
            st.error("Terraform executable is not available. Attempting to re-initialize...")
            with st.spinner("Setting up Terraform again..."):
                get_terraform_executable.clear()
            st.rerun()
        else:
            st.session_state.validated = True
            temp_dir = "terraform_project"
            os.makedirs(temp_dir, exist_ok=True)
            with open(os.path.join(temp_dir, "main.tf"), "w") as f:
                f.write(st.session_state.terraform_code)
            
            with st.spinner("Running `terraform init` and `validate`..."):
                # Run init first to download providers
                init_process = subprocess.run([terraform_executable_path, "init", "-no-color", "-upgrade"], cwd=temp_dir, capture_output=True, text=True)
                
                if init_process.returncode != 0:
                    st.session_state.validation_result = f"An error occurred during `terraform init`:\n{init_process.stderr}"
                    st.session_state.has_errors = True
                else:
                    # Run validate
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
                try:
                    # --- FIX: Sanitize code content to remove non-ASCII 'smart quotes' that cause encoding errors ---
                    sanitized_code = st.session_state.terraform_code.replace('“', '"').replace('”', '"').replace('’', "'").replace('—', '-')
                    sanitized_result = st.session_state.validation_result.replace('“', '"').replace('”', '"').replace('’', "'").replace('—', '-')

                    client = openai.OpenAI(api_key=openai_api_key)
                    system_prompt = "You are a Terraform code correction expert. The user will provide HCL code and a validation error. Fix the code to resolve the error. Only return the complete, corrected HCL code block without explanations."
                    # Using triple quotes for multi-line f-string.
                    correction_prompt = f"""**Terraform Code with Errors:**
```hcl
{sanitized_code}
```

**Validation Error:**
```
{sanitized_result}
```
Please provide the corrected code."""
                    
                    completion = client.chat.completions.create(model="gpt-4o", messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": correction_prompt}])
                    
                    if not (completion.choices and completion.choices[0].message and completion.choices[0].message.content):
                        st.error("API Error: Received an empty or malformed response from the OpenAI API during correction.")
                        response_content = ""
                    else:
                        response_content = completion.choices[0].message.content
                    
                    # Use a more forgiving regex to capture the code block content
                    code_match = re.search(r'```[a-zA-Z]*\n(.*?)\n```', response_content, re.DOTALL)
                    
                    if code_match:
                        st.session_state.terraform_code = code_match.group(1).strip()
                    else:
                        # Fallback: Use the entire response, but try to strip common fences
                        cleaned_content = response_content.strip()
                        cleaned_content = re.sub(r'^```[a-zA-Z]*\s*', '', cleaned_content)
                        cleaned_content = re.sub(r'```$', '', cleaned_content)
                        st.session_state.terraform_code = cleaned_content.strip()
                        st.warning("⚠️ The AI response did not contain a standard markdown code block. Using the raw output.")
                    
                    st.session_state.validation_result, st.session_state.has_errors, st.session_state.validated = "🔧 AI has attempted to correct the code. Please validate again.", False, False
                except openai.AuthenticationError:
                    st.error("Authentication Error: The OpenAI API key is invalid or has expired.")
                except Exception as e:
                    st.error(f"An error occurred during correction: {e}")
            st.rerun()

# --- Display Results Area ---
with col_results:
    st.header("Results")
    
    # Display raw response for debugging if extraction failed
    if st.session_state.raw_response_debug:
        st.subheader("⚠️ Raw Debug Response (API Output)")
        st.info("The code box is empty because the AI failed to respond with a usable code block or the API call failed. This is the raw text (or error message) received:")
        st.code(st.session_state.raw_response_debug, language="text")

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
