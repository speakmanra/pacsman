import logging
import os
import threading
from contextlib import contextmanager
from collections import defaultdict
from itertools import chain

from pydicom import dcmread
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom3 import AE, QueryRetrieveSOPClassList, StorageSOPClassList, \
    pynetdicom_version, pynetdicom_implementation_uid
from pynetdicom3.pdu_primitives import SCP_SCU_RoleSelectionNegotiation

from .dicom_interface import DicomInterface
from .utils import process_and_write_png, copy_dicom_attributes, \
    set_undefined_tags_to_blank


logger = logging.getLogger(__name__)


# http://dicom.nema.org/medical/dicom/current/output/html/part07.html#chapter_C
status_success_or_pending = [0x0000, 0xFF00, 0xFF01]


class PynetdicomClient(DicomInterface):
    def __init__(self, client_ae, pacs_url, pacs_port, dicom_dir, timeout=5,
                 *args, **kwargs):
        """
        :param client_ae: Name for this client Association Entity. {client_ae}-SCP:11113
            needs to be registered with the remote PACS in order for C-MOVE to work
        :param pacs_url: Remote PACS URL
        :param pacs_port: Remote PACS port (usually 11112)
        :param dicom_dir: Root dir for storage of *.dcm files.
        :param timeout: Connection and DICOM timeout in seconds
        """
        self.client_ae = client_ae
        self.pacs_url = pacs_url
        self.pacs_port = pacs_port
        self.dicom_dir = dicom_dir
        self.timeout = timeout

    def verify(self):

        ae = AE(ae_title=self.client_ae, scu_sop_class=['1.2.840.10008.1.1'])
        # setting timeout here doesn't appear to have any effect
        ae.network_timeout = self.timeout

        with association(ae, self.pacs_url, self.pacs_port) as assoc:
            logger.debug('Association accepted by the peer')
            # Send a DIMSE C-ECHO request to the peer
            # status is a pydicom Dataset object with (at a minimum) a
            # (0000, 0900) Status element
            status = assoc.send_c_echo()

            # Output the response from the peer
            if status.Status in status_success_or_pending:
                logger.debug('C-ECHO Response: 0x{0:04x}'.format(status.Status))
                return True
            else:
                logger.warning('C-ECHO Failure Response: 0x{0:04x}'.format(status.Status))
                return False

        return False

    def search_patients(self, search_query, additional_tags=None):
        ae = AE(ae_title=self.client_ae, scu_sop_class=QueryRetrieveSOPClassList)

        with association(ae, self.pacs_url, self.pacs_port) as assoc:
            search_query = f'*{search_query}*'
            id_responses = _find_patients(assoc, 'PatientID', search_query, additional_tags)
            name_responses = _find_patients(assoc, 'PatientName', search_query, additional_tags)
            responses = checked_responses(chain(id_responses, name_responses))

            patient_id_to_datasets = defaultdict(Dataset)
            for study in responses:
                result = patient_id_to_datasets[study.PatientID]
                self.build_patient_result(result, study, additional_tags)
            return list(patient_id_to_datasets.values())

    def studies_for_patient(self, patient_id, additional_tags=None):
        ae = AE(ae_title=self.client_ae, scu_sop_class=QueryRetrieveSOPClassList)

        with association(ae, self.pacs_url, self.pacs_port) as assoc:
            responses = _find_patients(assoc, 'PatientID', f'{patient_id}', additional_tags)

            datasets = []
            for dataset in checked_responses(responses):
                # Some PACS send back empty "Success" responses at the end of the list
                if hasattr(dataset, 'PatientID'):
                    datasets.append(dataset)

            return datasets

    def search_series(self, query_dataset, additional_tags=None):
        additional_tags = additional_tags or []
        query_dataset.QueryRetrieveLevel = 'INSTANCE'
        additional_tags += [
            'Modality',
            'BodyPartExamined',
            'SeriesDescription',
            'SeriesDate',
            'SeriesTime',
            'PatientPosition',
        ]
        set_undefined_tags_to_blank(query_dataset, additional_tags)
        ae = AE(ae_title=self.client_ae, scu_sop_class=QueryRetrieveSOPClassList)

        datasets = []
        with association(ae, self.pacs_url, self.pacs_port) as assoc:
            responses = assoc.send_c_find(query_dataset, query_model='S')
            for series in checked_responses(responses):
                if hasattr(series, 'SeriesInstanceUID'):
                    datasets.append(series)
        return datasets

    def series_for_study(self, study_id, modality_filter=None, additional_tags=None):
        additional_tags = additional_tags or []

        ae = AE(ae_title=self.client_ae, scu_sop_class=QueryRetrieveSOPClassList)

        with association(ae, self.pacs_url, self.pacs_port) as assoc:
            dataset = Dataset()
            dataset.StudyInstanceUID = study_id
            dataset.QueryRetrieveLevel = 'SERIES'

            additional_tags += [
                'SeriesInstanceUID',
                'BodyPartExamined',
                'SeriesDescription',
                'SeriesDate',
                'SeriesTime',
                'PatientPosition',
                'NumberOfSeriesRelatedInstances',
            ]
            set_undefined_tags_to_blank(dataset, additional_tags)
            # Filtering modality with 'MR\\CT' doesn't seem to work with pynetdicom
            dataset.Modality = ''

            responses = assoc.send_c_find(dataset, query_model='S')

            series_datasets = []
            for series in checked_responses(responses):
                valid_dicom = hasattr(series, 'SeriesInstanceUID')
                modality = getattr(series, 'Modality', '')
                match = modality_filter is None or modality in modality_filter
                if valid_dicom and match:
                    ds = Dataset()
                    ds.SeriesDescription = getattr(series, 'SeriesDescription', '')
                    ds.BodyPartExamined = getattr(series, 'BodyPartExamined', None)
                    ds.SeriesInstanceUID = series.SeriesInstanceUID
                    ds.Modality = series.Modality
                    ds.SeriesDate = series.SeriesDate
                    ds.SeriesTime = series.SeriesTime
                    copy_dicom_attributes(ds, series, additional_tags)

                    ds.NumberOfSeriesRelatedInstances = self._determine_number_of_images(ae, series)
                    series_datasets.append(ds)

        return series_datasets

    def _determine_number_of_images(self, ae, series):
        answer_from_instance_count = series.NumberOfSeriesRelatedInstances
        if answer_from_instance_count:
            return answer_from_instance_count
        else:
            return str(self._count_images_via_query(ae, series))

    def _count_images_via_query(self, ae, series):
        with association(ae, self.pacs_url, self.pacs_port) as series_assoc:
            series_dataset = Dataset()
            series_dataset.SeriesInstanceUID = series.SeriesInstanceUID
            series_dataset.QueryRetrieveLevel = 'IMAGE'
            series_dataset.SOPInstanceUID = ''

            series_responses = series_assoc.send_c_find(series_dataset, query_model='S')
            image_count = 0
            for instance in checked_responses(series_responses):
                if hasattr(instance, 'SOPInstanceUID'):
                    image_count += 1
        return image_count

    def images_for_series(self, series_id, additional_tags=None, max_count=None):
        ae = AE(ae_title=self.client_ae, scu_sop_class=QueryRetrieveSOPClassList)
        image_datasets = []
        with association(ae, self.pacs_url, self.pacs_port) as series_assoc:
            series_dataset = Dataset()
            series_dataset.SeriesInstanceUID = series_id
            series_dataset.QueryRetrieveLevel = 'IMAGE'
            series_dataset.SOPInstanceUID = ''
            set_undefined_tags_to_blank(series_dataset, additional_tags)

            series_responses = series_assoc.send_c_find(series_dataset, query_model='S')
            for instance in checked_responses(series_responses):
                if hasattr(instance, 'SOPInstanceUID'):
                    ds = Dataset()
                    ds.SeriesInstanceUID = instance.SeriesInstanceUID
                    ds.SOPInstanceUID = instance.SOPInstanceUID
                    copy_dicom_attributes(ds, instance, additional_tags)
                    image_datasets.append(ds)
                    if max_count and len(image_datasets) >= max_count:
                        break
        return image_datasets

    def fetch_images_as_dicom_files(self, series_id):

        series_path = os.path.join(self.dicom_dir, series_id)

        with storage_scp(self.client_ae, series_path) as scp:
            ae = AE(ae_title=self.client_ae,
                    scu_sop_class=QueryRetrieveSOPClassList,
                    transfer_syntax=[ExplicitVRLittleEndian])

            extended_negotiation_info = []
            for context in ae.presentation_contexts_scu:
                negotiation = SCP_SCU_RoleSelectionNegotiation()
                negotiation.sop_class_uid = context.abstract_syntax
                negotiation.scu_role = False
                negotiation.scp_role = True
                extended_negotiation_info.append(negotiation)

            with association(ae, self.pacs_url, self.pacs_port,
                             ext_neg=extended_negotiation_info) as assoc:
                dataset = Dataset()
                dataset.SeriesInstanceUID = series_id
                dataset.QueryRetrieveLevel = 'IMAGE'

                if scp.is_alive():
                    responses = assoc.send_c_move(dataset, scp.ae_title, query_model='S')
                else:
                    raise Exception(f'Storage SCP failed to start for series {series_id}')

                check_responses(responses)
                return series_path if os.path.exists(series_path) else None

    def fetch_image_as_dicom_file(self, series_id, sop_instance_id):
        series_path = os.path.join(self.dicom_dir, series_id)
        with storage_scp(self.client_ae, series_path) as scp:
            ae = AE(ae_title=self.client_ae,
                    scu_sop_class=QueryRetrieveSOPClassList,
                    transfer_syntax=[ExplicitVRLittleEndian])

            extended_negotiation_info = []
            for context in ae.presentation_contexts_scu:
                negotiation = SCP_SCU_RoleSelectionNegotiation()
                negotiation.sop_class_uid = context.abstract_syntax
                negotiation.scu_role = False
                negotiation.scp_role = True
                extended_negotiation_info.append(negotiation)

            with association(ae, self.pacs_url, self.pacs_port,
                             ext_neg=extended_negotiation_info) as assoc:
                dataset = Dataset()
                dataset.SeriesInstanceUID = series_id
                dataset.SOPInstanceUID = sop_instance_id
                dataset.QueryRetrieveLevel = 'IMAGE'

                if scp.is_alive():
                    responses = assoc.send_c_move(dataset, scp.ae_title,
                                                  query_model='S')
                else:
                    raise Exception(f'Storage SCP failed to start for series {series_id}')

                check_responses(responses)
                filepath = scp.path_for_dataset_instance(dataset)
                return filepath if os.path.exists(filepath) else None
        return None

    def fetch_thumbnail(self, series_id):
        ae = AE(ae_title=self.client_ae, scu_sop_class=QueryRetrieveSOPClassList)

        with association(ae, self.pacs_url, self.pacs_port) as assoc:
            # search for image IDs in the series
            find_dataset = Dataset()
            find_dataset.SeriesInstanceUID = series_id
            find_dataset.QueryRetrieveLevel = 'IMAGE'
            find_dataset.SOPInstanceUID = ''
            find_response = assoc.send_c_find(find_dataset, query_model='S')

            image_ids = []
            for dataset in checked_responses(find_response):
                if hasattr(dataset, 'SOPInstanceUID'):
                    image_ids.append(dataset.SOPInstanceUID)

            if not image_ids:
                return None

            with storage_scp(self.client_ae, self.dicom_dir) as scp:
                # try to get the middle image in the series for the thumbnail:
                #  instance ID order is usually the same as slice order but not guaranteed
                #  by the standard.
                middle_image_id = image_ids[len(image_ids) // 2]
                move_dataset = Dataset()
                move_dataset.SOPInstanceUID = middle_image_id
                move_dataset.QueryRetrieveLevel = 'IMAGE'

                if scp.is_alive():
                    move_responses = assoc.send_c_move(move_dataset, scp.ae_title,
                                                       query_model='S')
                else:
                    raise Exception(f'Storage SCP failed to start for series {series_id}')

                check_responses(move_responses)

                dcm_path = os.path.join(self.dicom_dir, f'{middle_image_id}.dcm')
                if not os.path.exists(dcm_path):
                    return None

                try:
                    thumbnail_ds = dcmread(dcm_path)
                    png_path = os.path.splitext(dcm_path)[0] + '.png'
                    process_and_write_png(thumbnail_ds, png_path)
                finally:
                    os.remove(dcm_path)
                return png_path


def _find_patients(assoc, search_field, search_query, additional_tags=None):
    dataset = Dataset()

    dataset.PatientID = None
    dataset.PatientName = ''
    dataset.PatientBirthDate = None
    dataset.StudyDate = ''
    dataset.StudyInstanceUID = ''
    dataset.QueryRetrieveLevel = 'STUDY'

    setattr(dataset, search_field, search_query)

    set_undefined_tags_to_blank(dataset, additional_tags)

    return assoc.send_c_find(dataset, query_model='S')


class StorageSCP(threading.Thread):
    def __init__(self, client_ae, result_dir):
        self.result_dir = result_dir

        self.ae_title = f'{client_ae}-SCP'
        self.ae = AE(ae_title=self.ae_title,
                     port=11113,
                     transfer_syntax=[ExplicitVRLittleEndian],
                     scp_sop_class=[x for x in StorageSOPClassList])

        self.ae.on_c_store = self._on_c_store

        threading.Thread.__init__(self)

        self.daemon = True

    def run(self):
        """The thread run method"""
        self.ae.start()

    def stop(self):
        """Stop the SCP thread"""
        self.ae.stop()

    def path_for_dataset_instance(self, dataset):
        filename = f'{dataset.SOPInstanceUID}.dcm'
        return os.path.join(self.result_dir, filename)

    def _on_c_store(self, dataset, context, info):
        '''
        :param dataset: pydicom.Dataset
            The DICOM dataset sent via the C-STORE
        :param context: pynetdicom3.presentation.PresentationContextTuple
            Details of the presentation context the dataset was sent under.
        :param info: dict
            A dict containing information about the association and DIMSE message.
        :return: pynetdicom.sop_class.Status or int
        '''
        try:

            os.makedirs(self.result_dir, exist_ok=True)

            filepath = self.path_for_dataset_instance(dataset)

            logger.info(f'Storing DICOM file: {filepath}')

            if os.path.exists(filepath):
                logger.warning('DICOM file already exists, overwriting')

            meta = Dataset()
            meta.MediaStorageSOPClassUID = dataset.SOPClassUID
            meta.MediaStorageSOPInstanceUID = dataset.SOPInstanceUID
            meta.ImplementationClassUID = pynetdicom_implementation_uid
            meta.TransferSyntaxUID = context.transfer_syntax

            # The following is not mandatory, set for convenience
            meta.ImplementationVersionName = pynetdicom_version

            ds = FileDataset(filepath, {}, file_meta=meta, preamble=b"\0" * 128)
            ds.update(dataset)
            ds.is_little_endian = context.transfer_syntax.is_little_endian

            ds.is_implicit_VR = context.transfer_syntax.is_implicit_VR
            ds.save_as(filepath, write_like_original=False)

            status_ds = Dataset()
            status_ds.Status = 0x0000
        except Exception as e:
            logger.error(f'C-STORE failed: {e}')
            status_ds = Dataset()
            status_ds.Status = 0x0110  # Processing Failure
        return status_ds


@contextmanager
def association(ae, pacs_url, pacs_port, *args, **kwargs):
    try:
        assoc = ae.associate(pacs_url, pacs_port, *args, **kwargs)
        if assoc.is_established:
            yield assoc
        elif assoc.is_rejected:
            raise ConnectionError(f'Association rejected with {pacs_url}')
        elif assoc.is_aborted:
            raise ConnectionError(f'Received A-ABORT during association with {pacs_url}')
        else:
            raise ConnectionError(f'Failed to establish association with {pacs_url}')
    except Exception as e:
        raise e
    finally:
        assoc.release()


@contextmanager
def storage_scp(client_ae, result_dir):
    try:
        scp = StorageSCP(client_ae, result_dir)
        scp.start()
        yield scp
    except Exception as e:
        raise e
    finally:
        scp.stop()


def checked_responses(responses):
    '''
    Generator for checking success or pending status of DICOM responses
    Success response may only come once at the end of the dataset response list.

    :param responses: List of (Status, Dataset) tuples from pynetdicom call
    :return: List of Datasets or exception on warning/abort/failure
    '''
    for (status, dataset) in responses:
        logger.debug(status)
        logger.debug(dataset)
        if status.Status in status_success_or_pending:
            yield dataset
        else:
            raise Exception('DICOM Response Failed With Status: 0x{0:04x}'.format(status.Status))


def check_responses(responses):
    for _ in checked_responses(responses):
        pass
