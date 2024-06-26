import math

from PIL import Image
from PIL import ImageFilter
from PIL import ImageEnhance

from modules import shared
from modules import devices

from sd_bmab import util, masking
from sd_bmab import external
from sd_bmab.base import process_img2img, Context, ProcessorBase, VAEMethodOverride

from sd_bmab.util import debug_print
from sd_bmab.detectors import UltralyticsPersonDetector8n
from sd_bmab import constants


class PersonDetailer(ProcessorBase):
	def __init__(self) -> None:
		super().__init__()
		self.detailing_opt = None
		self.parameters = None

		self.dilation = 3
		self.area_ratio = 0.1
		self.limit = 1
		self.force_one_on_one = False
		self.background_color = 1
		self.background_blur = 0
		self.use_groudingdino = False
		self.detection_model = 'Ultralytics(person_yolov8n-seg.pt)'
		self.max_element = shared.opts.bmab_max_detailing_element
		self.checkpoint = constants.checkpoint_default
		self.vae = constants.vae_default
		self.sampler = constants.sampler_default
		self.scheduler = constants.scheduler_default

	def preprocess(self, context: Context, image: Image):
		if context.args['person_detailing_enabled']:
			self.detailing_opt = context.args.get('module_config', {}).get('person_detailing_opt', {})
			self.parameters = dict(context.args.get('module_config', {}).get('person_detailing', {}))
			self.dilation = self.detailing_opt.get('dilation', self.dilation)
			self.area_ratio = self.detailing_opt.get('area_ratio', self.area_ratio)
			self.limit = self.detailing_opt.get('limit', self.limit)
			self.force_one_on_one = self.detailing_opt.get('force_1:1', self.force_one_on_one)
			self.background_color = self.detailing_opt.get('background_color', self.background_color)
			self.background_blur = self.detailing_opt.get('background_blur', self.background_blur)
			self.use_groudingdino = self.detailing_opt.get('use_groudingdino', self.use_groudingdino)
			self.detection_model = self.detailing_opt.get('detection_model', self.detection_model)
			self.checkpoint = self.detailing_opt.get('checkpoint', self.checkpoint)
			self.vae = self.detailing_opt.get('vae', self.vae)
			self.sampler = self.detailing_opt.get('sampler', self.sampler)
			self.scheduler = self.detailing_opt.get('scheduler', self.scheduler)

		return context.args['person_detailing_enabled']

	def get_cropped_mask(self, image, boxes, box):
		sam = masking.get_mask_generator()
		mask = sam.predict(image, boxes)
		mask = util.dilate_mask(mask, self.dilation)
		cropped_mask = mask.crop(box=box).convert('L')
		return cropped_mask

	def process(self, context: Context, image: Image):
		context.add_generation_param('BMAB_person_option', util.dict_to_str(self.detailing_opt))
		if self.use_groudingdino:
			text_prompt = "person . head . face . hand ."
			debug_print('prepare detector groundingdino')
			with external.ModuleAutoLoader('groundingdino', 'grdino') as dino:
				boxes, logits, phrases = dino.dino_predict(image, text_prompt, 0.30, 0.20)
		else:
			debug_print('prepare detector Ultralytics')
			detector = UltralyticsPersonDetector8n()
			boxes, logits = detector.predict(context, image)
			phrases = ['person'] * len(boxes)

		org_size = image.size
		debug_print('size', org_size)

		i2i_config = self.parameters
		debug_print(f'Max element {self.max_element}')

		context.add_job(min(self.limit, len(boxes)))

		if self.sampler != constants.sampler_default:
			i2i_config['sampler_name'] = self.sampler
		if self.scheduler != constants.scheduler_default:
			i2i_config['scheduler'] = self.scheduler

		processed = []
		for idx, (box, logit, phrase) in enumerate(zip(boxes, logits, phrases)):
			if phrase != 'person':
				continue

			if self.limit != 0 and idx >= self.limit:
				debug_print(f'Over limit {self.limit}')
				break

			if self.max_element != 0 and idx >= self.max_element:
				debug_print(f'Over limit MAX Element {self.max_element}')
				break

			debug_print('render', float(logit))
			box2 = util.fix_box_size(box)
			x1, y1, x2, y2 = box2

			cropped = image.crop(box=box)

			scale = self.detailing_opt.get('scale', 4)
			if self.force_one_on_one:
				scale = 1.0

			area_person = cropped.width * cropped.height
			area_image = image.width * image.height
			ratio = area_person / area_image
			debug_print(f'Ratio {ratio}')
			if scale > 1 and ratio >= self.area_ratio:
				debug_print(f'Person is too big to process. {ratio} >= {self.area_ratio}.')
				if self.background_color != 1:
					cropped_mask = self.get_cropped_mask(image, boxes, box)
					processed.append((cropped, (x1, y1), cropped_mask))
					continue
				context.add_generation_param(
					'BMAB_person_SKIP', f'Person is too big to process. {ratio} >= {self.area_ratio}.')
				return image

			context.add_generation_param('BMAB person ratio', '%.3f' % ratio)

			w = int(cropped.width * scale)
			h = int(cropped.height * scale)
			debug_print(f'Trying x{scale} ({cropped.width},{cropped.height}) -> ({w},{h})')

			if scale > 1 and self.detailing_opt.get('block_overscaled_image', True):
				area_org = context.get_max_area()
				area_scaled = w * h
				if area_scaled > area_org:
					debug_print(f'It is too large to process.')
					auto_upscale = self.detailing_opt.get('auto_upscale', True)
					if not auto_upscale:
						if self.background_color != 1:
							cropped_mask = self.get_cropped_mask(image, boxes, box)
							processed.append((cropped, (x1, y1), cropped_mask))
							continue
						context.add_generation_param('BMAB_person_SKIP', f'It is too large to process.')
						return image
					scale = math.sqrt(area_org / (cropped.width * cropped.height))
					w, h = util.fix_size_by_scale(cropped.width, cropped.height, scale)
					debug_print(f'Auto Scale x{scale} ({cropped.width},{cropped.height}) -> ({w},{h})')
					if scale < 1.2:
						debug_print(f'Scale {scale} has no effect. skip!!!!!')
						if self.background_color != 1:
							cropped_mask = self.get_cropped_mask(image, boxes, box)
							processed.append((cropped, (x1, y1), cropped_mask))
							continue
						context.add_generation_param('BMAB_person_SKIP', f'Scale {scale} has no effect. skip!!!!!')
						return image

			cropped_mask = self.get_cropped_mask(image, boxes, box)
			options = dict(mask=cropped_mask, **i2i_config)
			options['width'] = w
			options['height'] = h
			options['inpaint_full_res'] = 1
			options['inpaint_full_res_padding'] = 32

			if context.is_hires_fix():
				options['prompt'] = context.get_hires_prompt_by_index()
				debug_print(options['prompt'])

			if self.checkpoint is not None and self.checkpoint != constants.checkpoint_default:
				override_settings = options.get('override_settings', {})
				override_settings['sd_model_checkpoint'] = self.checkpoint
				options['override_settings'] = override_settings
			if self.vae != constants.vae_default:
				override_settings = options.get('override_settings', {})
				override_settings['sd_vae'] = self.vae
				options['override_settings'] = override_settings

			with VAEMethodOverride():
				img2img_result = process_img2img(context, cropped, options=options)
			img2img_result = img2img_result.resize(cropped.size, resample=util.LANCZOS)
			blur = ImageFilter.GaussianBlur(3)
			cropped_mask = cropped_mask.filter(blur)
			processed.append((img2img_result, (x1, y1), cropped_mask))

		if self.background_color != 1:
			enhancer = ImageEnhance.Color(image)
			image = enhancer.enhance(self.background_color)
		if self.background_blur > 3:
			blur = ImageFilter.GaussianBlur(self.background_blur)
			image = image.filter(blur)

		for img2img_result, pos, cropped_mask in processed:
			image.paste(img2img_result, pos, mask=cropped_mask)

		return image

	def postprocess(self, context: Context, image: Image):
		devices.torch_gc()
