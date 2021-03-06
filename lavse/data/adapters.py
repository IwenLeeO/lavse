from ..utils.file_utils import load_json
from collections import defaultdict
from ..utils.logger import get_logger
from pathlib import Path


logger = get_logger()


class Flickr:

    def __init__(self, data_path, data_split):

        data_split = data_split.replace('dev', 'val')

        self.data_path = Path(data_path)
        self.annotation_path = (
            self.data_path / 'dataset_flickr30k.json'
        )
        self.data = load_json(self.annotation_path)

        img_dict = {}
        annotations = defaultdict(list)
        self.image_ids = []

        for img in self.data['images']:
            if img['split'].lower() != data_split.lower():
                continue
            img_dict[img['imgid']] = img
            self.image_ids.append(img['imgid'])

            annotations[img['imgid']].extend(
                [x['raw'] for x in img['sentences']][:5]
            )

        for k, v in annotations.items():
            assert len(v) == 5

        self.image_captions = annotations
        self.img_dict = img_dict
        logger.info((
            f'[Flickr] Loaded {len(self.image_captions)} images '
            f'and {len(self.image_captions)*5} annotations.'
        ))

    def get_image_id_by_filename(self, filename):
        return self.img_dict[filename]['imgid']

    def get_captions_by_image_id(self, img_id):
        return self.image_captions[img_id]

    def get_filename_by_image_id(self, image_id):
        return (
            Path('images') /
            Path('flickr30k_images') /
            self.img_dict[image_id]['filename']
        )

    def __call__(self, filename):
        return self.img_dict[filename]

    def __len__(self, ):
        return len(self.image_captions)


class Coco:

    def __init__(self, path, data_split):

        data_split = data_split.replace('dev', 'val')

        self.data_path = Path(path)
        self.annotation_path = (
            self.data_path / 'dataset_coco.json'
        )
        self.data = load_json(self.annotation_path)

        img_dict = {}
        annotations = defaultdict(list)
        self.image_ids = []

        for img in self.data['images']:
            split = img['split'].lower().replace('restval', 'train')
            if split != data_split.lower():
                continue
            img_dict[img['imgid']] = img
            self.image_ids.append(img['imgid'])

            annotations[img['imgid']].extend(
                [x['raw'] for x in img['sentences']][:5]
            )

        for k, v in annotations.items():
            assert len(v) == 5

        self.image_captions = annotations
        self.img_dict = img_dict
        logger.info((
            f'[Coco] Loaded {len(self.image_captions)} images '
            f'and {len(self.image_captions)*5} annotations.'
        ))

    def get_image_id_by_filename(self, filename):
        return self.img_dict[filename]['imgid']

    def get_captions_by_image_id(self, img_id):
        return self.image_captions[img_id]

    def get_filename_by_image_id(self, image_id):
        return (
            Path('images') /
            self.img_dict[image_id]['filename'].split('_')[1] /
            self.img_dict[image_id]['filename']
        )

    def __call__(self, filename):
        return self.img_dict[filename]

    def __len__(self, ):
        return len(self.image_captions)
