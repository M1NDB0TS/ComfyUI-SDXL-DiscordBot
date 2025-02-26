import configparser
import json
import tempfile

from PIL import Image

from src.comfy_api import ComfyGenerator as ImageGenerator, upload_image, clear_cache
from src.image_gen.ImageWorkflow import ImageWorkflow

# Read the configuration
config = configparser.ConfigParser()
config.read("config.properties")
server_address = config["LOCAL"]["SERVER_ADDRESS"]
previous_workflow = None


def setup_workflow(workflow, params: ImageWorkflow):
    global previous_workflow
    if previous_workflow is not None and params.workflow_name != previous_workflow:
        clear_cache()

    prompt_nodes = config.get(params.workflow_name, "PROMPT_NODES").split(",")
    neg_prompt_nodes = config.get(params.workflow_name, "NEG_PROMPT_NODES").split(",")
    rand_seed_nodes = config.get(params.workflow_name, "RAND_SEED_NODES").split(",")
    model_node = config.get(params.workflow_name, "MODEL_NODE").split(",") if config.has_option(params.workflow_name, "MODEL_NODE") else []
    lora_node = config.get(params.workflow_name, "LORA_NODE").split(",") if config.has_option(params.workflow_name, "LORA_NODE") else []
    clip_skip_node = config.get(params.workflow_name, "CLIP_SKIP_NODE") if config.has_option(params.workflow_name, "CLIP_SKIP_NODE") else None
    llm_model_node = None
    turbo_lora_node = None
    inpainting_node = None
    inpainting_threshold_node = None

    if config.has_option(params.workflow_name, "FILE_INPUT_NODES"):
        file_input_nodes = config.get(params.workflow_name, "FILE_INPUT_NODES").split(",")

    if config.has_option(params.workflow_name, "LLM_MODEL_NODE"):
        llm_model_node = config.get(params.workflow_name, "LLM_MODEL_NODE")

    if config.has_option(params.workflow_name, "TURBO_LORA_NODE"):
        turbo_lora_node = config.get(params.workflow_name, "TURBO_LORA_NODE")

    if config.has_option(params.workflow_name, "INPAINTING_PROMPT_NODE"):
        inpainting_node = config.get(params.workflow_name, "INPAINTING_PROMPT_NODE")
        inpainting_threshold_node = config.get(params.workflow_name, "INPAINTING_THRESHOLD_NODE")

    # Modify the prompt dictionary
    if params.prompt is not None and prompt_nodes[0] != "":
        for node in prompt_nodes:
            if "text" in workflow[node]["inputs"]:
                workflow[node]["inputs"]["text"] = params.prompt
            elif "prompt" in workflow[node]["inputs"]:
                workflow[node]["inputs"]["prompt"] = params.prompt

    if neg_prompt_nodes[0] != "":
        neg_prompt = (params.negative_prompt or "")
        for node in neg_prompt_nodes:
            if "text" in workflow[node]["inputs"]:
                workflow[node]["inputs"]["text"] = neg_prompt
            elif "prompt" in workflow[node]["inputs"]:
                workflow[node]["inputs"]["prompt"] = neg_prompt

    if params.filename is not None and config.has_option(params.workflow_name, "FILE_INPUT_NODES"):
        for node in file_input_nodes:
            workflow[node]["inputs"]["image"] = params.filename

    if rand_seed_nodes[0] != "":
        for node in rand_seed_nodes:
            workflow[node]["inputs"]["seed"] = params.seed

    if model_node is not None and params.model is not None and len(model_node) != 0 and model_node[0] != "":
        for node in model_node:
            if "ckpt_name" in workflow[node]["inputs"]:
                workflow[node]["inputs"]["ckpt_name"] = params.model
            elif "base_ckpt_name" in workflow[node]["inputs"]:
                workflow[node]["inputs"]["base_ckpt_name"] = params.model

    if params.loras is not None and lora_node[0] != "":
        lora_strengths = params.lora_strengths or [1.0] * len(params.loras)
        for i, lora in enumerate(params.loras):
            if lora is None:
                continue
            for node in lora_node:
                istr = str(i + 1)
                if "lora_name_" + istr in workflow[node]["inputs"]:
                    workflow[node]["inputs"]["switch_" + istr] = "On"
                    workflow[node]["inputs"]["lora_name_" + istr] = lora
                    workflow[node]["inputs"]["model_weight_" + istr] = lora_strengths[i]
                    workflow[node]["inputs"]["clip_weight_" + istr] = lora_strengths[i]
                elif "lora_0" + istr in workflow[node]["inputs"]:
                    workflow[node]["inputs"]["lora_0" + istr] = lora
                    workflow[node]["inputs"]["strength_0" + istr] = lora_strengths[i]

    if llm_model_node is not None:
        workflow[llm_model_node]["inputs"]["model_dir"] = config["LOCAL"]["LLM_MODEL_LOCATION"]

    if params.aspect_ratio is not None and config.has_option(params.workflow_name, "EMPTY_IMAGE_NODE"):
        empty_image_node = config.get(params.workflow_name, "EMPTY_IMAGE_NODE").split(",")
        for node in empty_image_node:
            if "dimensions" in workflow[node]["inputs"]:
                workflow[node]["inputs"]["dimensions"] = params.aspect_ratio
            else:
                w = int(params.aspect_ratio.lstrip().split("x")[0])
                h = int(params.aspect_ratio.split("x")[1].lstrip().split(" ")[0])
                workflow[node]["inputs"]["empty_latent_width"] = w
                workflow[node]["inputs"]["empty_latent_height"] = h

    # maybe set sampler arguments
    sampler_args_given = params.denoise_strength is not None or params.sampler is not None or params.num_steps is not None or params.cfg_scale is not None
    if sampler_args_given and config.has_option(params.workflow_name, "DENOISE_NODE"):
        denoise_node = config.get(params.workflow_name, "DENOISE_NODE").split(",")
        for node in denoise_node:
            default_args = workflow[node]["inputs"]
            steps = params.num_steps or default_args["steps"]
            cfg = params.cfg_scale or default_args["cfg"]
            sampler = params.sampler or default_args["sampler_name"]
            workflow[node]["inputs"]["steps"] = steps
            workflow[node]["inputs"]["cfg"] = cfg
            workflow[node]["inputs"]["sampler_name"] = sampler
            # workaround for samplers that don't have a denoise input
            if "denoise" in default_args:
                denoise = params.denoise_strength or default_args["denoise"]
                workflow[node]["inputs"]["denoise"] = denoise

    # limit batch size to 1 if denoise strength is given ()
    if params.batch_size is not None and config.has_option(params.workflow_name, "LATENT_IMAGE_NODE"):
        latent_image_node = config.get(params.workflow_name, "LATENT_IMAGE_NODE").split(",")
        for node in latent_image_node:
            workflow[node]["inputs"]["amount"] = params.batch_size

    if turbo_lora_node is not None and config.get("SDXL_GENERATION_DEFAULTS", "TURBO_ENABLED") == "False":
        if "lora_01" in workflow[turbo_lora_node]["inputs"]:
            workflow[turbo_lora_node]["inputs"]["lora_01"] = "None"
        elif "switch_1" in workflow[turbo_lora_node]["inputs"]:
            workflow[turbo_lora_node]["inputs"]["switch_1"] = "Off"

    if params.inpainting_prompt is not None and inpainting_node is not None:
        workflow[inpainting_node]["inputs"]["text"] = params.inpainting_prompt
        workflow[inpainting_threshold_node]["inputs"]["threshold"] = params.inpainting_detection_threshold

    if clip_skip_node is not None:
        workflow[clip_skip_node]["inputs"]["stop_at_clip_layer"] = params.clip_skip

    previous_workflow = params.workflow_name

    print(params.workflow_name, workflow)

    return workflow


async def generate_images(params: ImageWorkflow):
    print("queuing workflow:", params)
    with open(config[params.workflow_name]["CONFIG"], "r") as file:
        workflow = json.load(file)

    generator = ImageGenerator()
    await generator.connect()

    setup_workflow(workflow, params)

    output, enhanced_prompt = await generator.get_images(workflow)
    await generator.close()

    images = [*output["images"], *output["gifs"]]
    return images, enhanced_prompt


async def generate_alternatives(params: ImageWorkflow, image: Image.Image):
    print("queuing workflow:", params)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as temp_file:
        image.save(temp_file, format="PNG")
        temp_filepath = temp_file.name

    # Upload the temporary file using the upload_image method
    response_data = upload_image(temp_filepath)
    filename = response_data["name"]
    params.filename = filename

    with open(config[params.workflow_name]["CONFIG"], "r") as file:
        workflow = json.load(file)

    generator = ImageGenerator()
    await generator.connect()

    setup_workflow(workflow, params)

    output, enhanced_prompt = await generator.get_images(workflow)
    await generator.close()

    images = [*output["images"], *output["gifs"]]
    return images


async def upscale_image(image: Image.Image, workflow_name: str = "LOCAL_UPSCALE"):
    print("queuing workflow:", workflow_name)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as temp_file:
        image.save(temp_file, format="PNG")
        temp_filepath = temp_file.name

    # Upload the temporary file using the upload_image method
    response_data = upload_image(temp_filepath)
    filename = response_data["name"]
    with open(config[workflow_name]["CONFIG"], "r") as file:
        workflow = json.load(file)

    generator = ImageGenerator()
    await generator.connect()

    file_input_nodes = config.get(workflow_name, "FILE_INPUT_NODES").split(",")

    for node in file_input_nodes:
        workflow[node]["inputs"]["image"] = filename

    output, enhanced_prompt = await generator.get_images(workflow)
    await generator.close()

    images = [*output["images"], *output["gifs"]]
    return images[0]
