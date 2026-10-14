# Smart Printer Assistant

A modern, local application that allows users to control and orchestrate local network printers using natural language. Instead of managing complex printer drivers, split print jobs, or manual color parsing, a Google Gemini Large Language Model (LLM) serves as the orchestrator to automate printing flows.

## Architecture and Design Principles

The project enforces a strict, secure layering architecture to isolate the LLM from structural and network hardware execution:

```text
[Streamlit UI/Dashboard]
       │
       ▼ (Invokes Gemini Model for a Plan)
[Gemini Orchestrator] 
       │
       ▼ (Returns Structured Tool Call/Plan)
[Streamlit UI/Dashboard]
       │
       ▼ (Validates & Routes Plan via Stdio)
[MCP Printer Server (printer_server.py)]
       │
       ├──(Method 1: Network IPP / Spooler over HTTP/Mime)──► [Network Printer]
       └──(Method 2: Native Windows GDI Fallback)───────────► [Windows Print Spooler]
```

1. **Strict Layering Protocol**: The LLM does not interact with the network or the printer directly. It receives plain text commands, document layout contexts, and device diagnostics, returning a highly structured execution scheme via Google GenAI Function Calling.
2. **Model Safety Sandbox**: The underlying `app.py` context strictly validates page boundaries and string formatting derived from the model before passing execution details down to the server binary. Untrusted text inputs meant to cause prompt injection are ignored.
3. **Robust Dual-Pipeline Fallback**: Print jobs communicate directly via Internet Printing Protocol (IPP). If the network endpoint or local IPP implementation encounters transport drops, it falls back to the native Windows print subsystems (`pywin32` / GDI spooler).

---

## Directory Layout

```text
.
├── app.py                 # Core Streamlit Web Application and Gemini orchestrator
├── document_scanner.py    # Document analyzer extracting chroma metrics per page
├── printer_server.py      # Model Context Protocol (MCP) tool host handling network IPP/Windows spoolers
├── requirements.txt       # Python environment dependencies pinning versions
└── .env.example           # Reference file detailing supported environment flags
```

---

## Core Component Detailed Summaries

### 1. `app.py`
The frontend presentation dashboard built with Streamlit. It orchestrates UI elements across split columns:
* **Diagnostics Panel**: Polls the Model Context Protocol (MCP) server asynchronously at configurable intervals (`DASHBOARD_REFRESH_SECONDS`) to fetch printer availability states, active system alerts, and real-time hex-coded ink bar percentages.
* **Command Console**: Accepts PDF file uploads and processes native English commands through `gemini-3.8-flash`. It matches structural page conditions against user text configurations and builds atomic monochrome or color sub-jobs.
* **Safety Verification**: Transforms raw model outputs into structured parameters, checking page boundaries against total document counts before initiating operations.

### 2. `document_scanner.py`
An automated optimization utility providing structural metrics for target documents before transmitting data profiles to the LLM:
* **Pixel Analysis Method**: Utilizes `pypdfium2` and `numpy` to map pages into raw array configurations. It flags color spaces by examining structural chroma deviations (`max(R,G,B) - min(R,G,B)`) across predefined thresholds.
* **Fallback Structural Method**: Runs an emergency parsing routine via `pypdf` if image rendering engines are missing. It evaluates structural operations inside raw page streams (`rg`, `RG`, `k`, `K` operators) alongside `/XObject` `/Image` sub-dictionaries to confirm color definitions.

### 3. `printer_server.py`
A low-level system layer conforming to the Model Context Protocol (MCP) spec running over a standard input/output (stdio) transport architecture:
* **Diagnostic Endpoint**: Leverages `pyipp` to query operational device profiles over TCP Port 631 or Port 80, parsing standard IPP printer state reasons and low ink constraints.
* **IPP Advanced Pipeline**: Manages printing tasks over high-performance connections. It delivers raw document layers when `application/pdf` profiles are verified by the hardware device. If unsupported (common with consumer-grade devices like HP Smart Tank targets), it dynamically translates segments into high-density JPEG formats (`image/jpeg`).
* **Windows Spooler Pipeline**: An industrial fallback layer acting on local system configurations. It builds structured Device Context arrays (`CreateDC`), translates page instances using Device Independent Bitmaps (`ImageWin.Dib`), and delivers tasks down to local spool structures.

---

## Installation and Setup

### Prerequisites
* Python 3.10 or higher.
* A valid Google Gemini API Key.
* Network access to an IPP-compliant printer or a printer configured inside the host Windows environment.

### Environment Preparation

Clone or download the project files into a local directory and configure a virtual environment:

```bash
python -m venv venv
source venv/bin/activate  # On Windows use: venv\Scripts\activate
pip install -r requirements.txt
```

### Environment Configuration

Create a `.env` file in the root directory. Use the structure below to specify target configuration flags:

```ini
# Gemini API Key configuration
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.8-flash

# Printer Connection Options (Leave IP empty to default purely to Windows Spooler)
PRINTER_IP=xx.xx.xx.xx
PRINTER_PORT=xxxx
IPP_PATH=/ipp/print
WINDOWS_PRINTER_NAME=xxxxxxxxx

# Web UI Tuning Parameters
DASHBOARD_REFRESH_SECONDS=15
MAX_UPLOAD_MB=50
UPLOAD_TTL_HOURS=24
REQUIRE_PRINT_CONFIRMATION=true

# Scan Profiles
COLOR_CHROMA_THRESHOLD=28.0
COLOR_PIXEL_FRACTION=0.0002
SCAN_RENDER_SCALE=0.75
IPP_RASTER_DPI=300
SPOOLER_DPI=200

# Logging Verbosity (DEBUG, INFO, WARNING, ERROR)
LOG_LEVEL=INFO
```

---

## Running the Application

To launch the local orchestrator dashboard, execute the primary application script via Streamlit:

```bash
streamlit run app.py
```

Open the local network URL output by the terminal (typically `http://localhost:8501`) to access the interface.

### Running the Diagnostic Utilities Directly

You can execute components directly through the command-line interface to confirm hardware parameters or verify local scanning profiles:

**To test the document color scanner performance:**
```bash
python document_scanner.py path/to/target_document.pdf
```

---

## Usage Guide

1. **Review System Readiness**: Ensure the left dashboard panel reports the printer status as `ONLINE`. Confirm that ink percentage levels render properly.
2. **Upload a File**: Provide a target document by dragging and dropping a PDF file into the command terminal zone. The scanner will run immediately to map page colors.
3. **Issue Natural Language Commands**: Type execution requests directly into the terminal interface. Examples include:
   * "Print only the pages that contain color graphics."
   * "Send pages 1 through 5 in monochrome, but print the final page using color."
   * "Print the black and white text pages now and skip everything else."
4. **Approve the Plan**: Review the execution scheme compiled inside the generated summary table. Click the confirmation button to dispatch data sequences through the active processing layers.
