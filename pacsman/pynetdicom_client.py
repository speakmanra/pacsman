from contextlib import contextmanager
from itertools import chain
import logging
import os
import threading

from dicom_interface import DicomInterface, PatientInfo, SeriesInfo
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import ImplicitVRLittleEndian, ExplicitVRLittleEndian
from pynetdicom3 import AE, QueryRetrieveSOPClassList, StorageSOPClassList, \
    pynetdicom_version, pynetdicom_implementation_uid
from pynetdicom3.pdu_primitives import SCP_SCU_RoleSelectionNegotiation

logger = logging.getLogger(__name__)

# http://dicom.nema.org/dicom/2013/output/chtml/part07/chapter_C.html
status_success_or_pending = [0x0000, 0xFF00, 0xFF01]


class PynetdicomClient(DicomInterface):

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

    def search_patients(self, search_query):

        ae = AE(ae_title=self.client_ae, scu_sop_class=QueryRetrieveSOPClassList)

        with association(ae, self.pacs_url, self.pacs_port) as assoc:
            # perform first search on patient ID
            id_responses = _call_c_find_patients(assoc, 'PatientID',
                                                 f'*{search_query}*')
            # perform second search on patient name
            name_responses = _call_c_find_patients(assoc, 'PatientName',
                                                   f'*{search_query}*')

            uid_to_result = {}
            for (status, result) in chain(id_responses, name_responses):
                logger.debug(status)
                logger.debug(result)
                if status.Status not in status_success_or_pending:
                    raise Exception('Patient C-FIND Failure Response: 0x{0:04x}'.format(status.Status))
                if result:
                    # remove non-unique Study UIDs
                    #  (some dupes are returned, especially for ID search)
                    uid_to_result[result.StudyInstanceUID] = result

            # separate by patient ID, count studies and get most recent
            patient_id_to_info = {}
            for study in uid_to_result.values():
                patient_id = study.PatientID
                study_id = study.StudyInstanceUID
                if patient_id in patient_id_to_info:
                    if study.StudyDate > patient_id_to_info[patient_id].most_recent_study:
                        most_recent_study = study.StudyDate
                    else:
                        most_recent_study = patient_id_to_info[patient_id].most_recent_study

                    prev_study_ids = patient_id_to_info[patient_id].study_ids

                    info = PatientInfo(first_name=study.PatientName.given_name,
                                       last_name=study.PatientName.family_name,
                                       dob=study.PatientBirthDate,
                                       patient_id=patient_id,
                                       most_recent_study=most_recent_study,
                                       study_ids=prev_study_ids + [study_id])
                else:
                    info = PatientInfo(first_name=study.PatientName.given_name,
                                       last_name=study.PatientName.family_name,
                                       dob=study.PatientBirthDate,
                                       patient_id=patient_id,
                                       most_recent_study=study.StudyDate,
                                       study_ids=[study_id])
                patient_id_to_info[patient_id] = info

            return list(patient_id_to_info.values())

    def studies_for_patient(self, patient_id):
        ae = AE(ae_title=self.client_ae, scu_sop_class=QueryRetrieveSOPClassList)

        with association(ae, self.pacs_url, self.pacs_port) as assoc:
            responses = _call_c_find_patients(assoc, 'PatientID', patient_id)

            study_ids = []
            for (status, result) in responses:
                if status.Status not in status_success_or_pending:
                    raise Exception('Studies C-FIND Failure Response: 0x{0:04x}'.format(status.Status))

                # Some PACS send back empty "Success" responses at the end of the list
                if hasattr(result, 'StudyInstanceUID'):
                    study_ids.append(result.StudyInstanceUID)

            return study_ids

    def series_for_study(self, study_id, modality_filter=None):

        ae = AE(ae_title=self.client_ae, scu_sop_class=QueryRetrieveSOPClassList)

        with association(ae, self.pacs_url, self.pacs_port) as assoc:
            dataset = Dataset()
            dataset.StudyInstanceUID = study_id

            # Filtering modality with 'MR\\CT' doesn't seem to work with pynetdicom
            dataset.Modality = ''
            dataset.PatientName = ''
            dataset.BodyPartExamined = ''
            dataset.SeriesDescription = ''
            dataset.SeriesDate = ''
            dataset.SeriesTime = ''
            dataset.SeriesInstanceUID = ''
            dataset.PatientPosition = ''
            dataset.QueryRetrieveLevel = 'SERIES'

            responses = assoc.send_c_find(dataset, query_model='S')

            series_infos = []
            for (status, series) in responses:
                logger.debug(status)
                logger.debug(series)

                if status.Status not in status_success_or_pending:
                    raise Exception('Series C-FIND Failure Response: 0x{0:04x}'.format(status.Status))

                if series and (modality_filter is None or
                               getattr(series, 'Modality', '') in modality_filter):
                    description = getattr(series, 'SeriesDescription', '')
                    body_part_examined = getattr(series, 'BodyPartExamined', None)
                    if body_part_examined:
                        description += f' ({body_part_examined})'

                    with association(ae, self.pacs_url, self.pacs_port) as series_assoc:
                        series_dataset = Dataset()
                        series_dataset.SeriesInstanceUID = series.SeriesInstanceUID
                        series_dataset.QueryRetrieveLevel = 'IMAGE'
                        series_dataset.SOPInstanceUID = ''

                        series_responses = series_assoc.send_c_find(series_dataset, query_model='S')
                        image_ids = []
                        for (instance_status, instance) in series_responses:
                            logger.debug(instance)
                            if instance_status.Status in status_success_or_pending:
                                if hasattr(instance, 'SOPInstanceUID'):
                                    image_ids.append(instance.SOPInstanceUID)
                            else:
                                raise Exception(
                                    'Image C-FIND Failure Response: 0x{0:04x}'.format(
                                        status.Status))

                    info = SeriesInfo(series_id=series.SeriesInstanceUID, description=description,
                                      modality=series.Modality, num_images=len(image_ids),
                                      acquisition_datetime=series.SeriesDate)

                    series_infos.append(info)

        return series_infos

    def fetch_images_as_files(self, series_id):

        series_path = os.path.join(self.dicom_dir, series_id)
        scp = StorageSCP(self.client_ae, series_path)
        scp.start()

        try:
            ae = AE(ae_title=self.client_ae,
                    scu_sop_class=QueryRetrieveSOPClassList,
                    transfer_syntax=[ExplicitVRLittleEndian])

            ext_neg = []
            for context in ae.presentation_contexts_scu:
                tmp = SCP_SCU_RoleSelectionNegotiation()
                tmp.sop_class_uid = context.abstract_syntax
                tmp.scu_role = False
                tmp.scp_role = True
                ext_neg.append(tmp)

            with association(ae, self.pacs_url, self.pacs_port, ext_neg=ext_neg) as assoc:
                dataset = Dataset()
                dataset.SeriesInstanceUID = series_id
                dataset.QueryRetrieveLevel = 'IMAGE'

                if scp.is_alive():
                    responses = assoc.send_c_move(dataset, scp.ae_title,
                                                  query_model='S')
                else:
                    raise Exception(f'Storage SCP failed to start for series {series_id}')

                for (status, response) in responses:
                    logger.debug(status)
                    logger.debug(response)

                    if status.Status not in status_success_or_pending:
                        raise Exception(
                            'Image C-MOVE Failure Response: 0x{0:04x}'.format(
                                status.Status))

                return series_path if os.path.exists(series_path) else None

        except Exception as e:
            raise e
        finally:
            scp.stop()

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
            for (status, result) in find_response:
                logger.debug(status)
                logger.debug(result)
                if status.Status in status_success_or_pending:
                    if hasattr(result, 'SOPInstanceUID'):
                        image_ids.append(result.SOPInstanceUID)
                else:
                    raise Exception('Thumbnail C-FIND Failure Response: 0x{0:04x}'.format(
                                    status.Status))

            if not image_ids:
                return None

            scp = StorageSCP(self.client_ae, self.dicom_dir)
            scp.start()
            try:
                # get the middle image in the series for the thumbnail
                middle_image_id = image_ids[len(image_ids) // 2]
                move_dataset = Dataset()
                move_dataset.SOPInstanceUID = middle_image_id
                move_dataset.QueryRetrieveLevel = 'IMAGE'

                if scp.is_alive():
                    response = assoc.send_c_move(move_dataset, scp.ae_title,
                                                 query_model='S')
                else:
                    raise Exception(f'Storage SCP failed to start for series {series_id}')

                for (status, d) in response:
                    logger.debug(status)
                    logger.debug(d)
                    if status.Status not in status_success_or_pending:
                        raise Exception(
                            'Thumbnail C-MOVE Failure Response: 0x{0:04x}'.format(
                                status.Status))

                result_path = os.path.join(self.dicom_dir, f'{middle_image_id}.dcm')
                return result_path if os.path.exists(result_path) else None
            except Exception as e:
                raise e
            finally:
                scp.stop()


def _call_c_find_patients(assoc, search_field, search_query):
    dataset = Dataset()

    dataset.PatientID = None
    dataset.PatientName = ''
    dataset.PatientBirthDate = None
    dataset.StudyDate = ''
    dataset.StudyInstanceUID = ''
    dataset.QueryRetrieveLevel = 'STUDY'

    setattr(dataset, search_field, search_query)

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

            filename = f'{dataset.SOPInstanceUID}.dcm'
            filepath = os.path.join(self.result_dir, filename)

            logger.info(f'Storing DICOM file: {filepath}')

            if os.path.exists(filename):
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
            return status_ds
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
    except:
        raise
    finally:
        assoc.release()
