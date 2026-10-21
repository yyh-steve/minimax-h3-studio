"""Explicit supported subset. Unknown or unsupported options never silently disappear."""
from .errors import ApiError
from .media import validate_source,MediaError

RATIOS=('adaptive','21:9','16:9','4:3','1:1','3:4','9:16')
STATES=('queued','running','succeeded','failed','cancelled')

def normalize_request(payload):
    def bad(message):raise ApiError(400,message)
    if not isinstance(payload,dict):bad('Expected a JSON object.')
    allowed={'model','content','duration','resolution','ratio','aigc_watermark'}
    if set(payload)-allowed:bad('Unsupported request fields. This release supports only single-image Ref2VA generation.')
    if payload.get('model')!='MiniMax-H3':bad('Only MiniMax-H3 is supported by this local backend.')
    if payload.get('resolution')!='768P':bad('Only resolution=768P is supported.')
    duration=payload.get('duration')
    if type(duration) is not int or not 4<=duration<=15:bad('duration must be an integer from 4 to 15.')
    ratio=payload.get('ratio','adaptive')
    if not isinstance(ratio,str) or ratio not in RATIOS:bad('Unsupported ratio.')
    if 'aigc_watermark' in payload and payload['aigc_watermark'] is not False:bad('aigc_watermark=true is not implemented; use false or omit it.')
    content=payload.get('content')
    if not isinstance(content,list) or len(content)!=2 or any(not isinstance(i,dict) for i in content):
        bad('content must contain exactly one text and one reference_image item.')
    texts=[i for i in content if i.get('type')=='text'];images=[i for i in content if i.get('type')=='image_url']
    if len(texts)!=1 or len(images)!=1:bad('Only one text and one reference image are supported.')
    text=texts[0];image=images[0]
    if set(text)!={'type','text'} or not isinstance(text.get('text'),str) or not text['text'].strip() or len(text['text'])>7000:
        bad('text must be nonempty and at most 7000 characters.')
    if set(image)!={'type','image_url','role'} or image.get('role')!='reference_image':
        bad('The image role must be explicitly reference_image; keyframes are not supported.')
    value=image.get('image_url')
    if not isinstance(value,dict) or set(value)!={'url'} or not isinstance(value['url'],str):bad('image_url must contain one string url.')
    try:validate_source(value['url'])
    except MediaError:bad('Use a supported image data URI or a public HTTPS image URL.')
    return {'model':'MiniMax-H3','text':text['text'],'image_source':value['url'],
            'duration':duration,'resolution':'768P','ratio':ratio}
