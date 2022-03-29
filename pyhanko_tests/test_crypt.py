import os
from io import BytesIO

import pytest

from pyhanko.pdf_utils import generic, misc, writer
from pyhanko.pdf_utils.crypt import (
    DEFAULT_CRYPT_FILTER,
    STD_CF,
    AuthStatus,
    CryptFilterConfiguration,
    IdentityCryptFilter,
    PubKeyAdbeSubFilter,
    PubKeyAESCryptFilter,
    PubKeyRC4CryptFilter,
    PubKeySecurityHandler,
    SecurityHandler,
    SecurityHandlerVersion,
    SerialisedCredential,
    SimpleEnvelopeKeyDecrypter,
    StandardAESCryptFilter,
    StandardRC4CryptFilter,
    StandardSecurityHandler,
    StandardSecuritySettingsRevision,
    build_crypt_filter,
)
from pyhanko.pdf_utils.generic import pdf_name
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.general import load_cert_from_pemder
from pyhanko_tests.samples import (
    MINIMAL_AES256,
    MINIMAL_ONE_FIELD,
    MINIMAL_ONE_FIELD_AES256,
    PDF_DATA_DIR,
    PUBKEY_SELFSIGNED_DECRYPTER,
    PUBKEY_TEST_DECRYPTER,
    TESTING_CA_DIR,
    VECTOR_IMAGE_PDF,
)


def _produce_legacy_encrypted_file(rev, keylen_bytes, use_aes):
    r = PdfFileReader(BytesIO(VECTOR_IMAGE_PDF))
    w = writer.PdfFileWriter()
    sh = StandardSecurityHandler.build_from_pw_legacy(
        rev, w._document_id[0].original_bytes, "ownersecret", "usersecret",
        keylen_bytes=keylen_bytes, use_aes128=use_aes,
        perms=-44
    )
    w.security_handler = sh
    w._encrypt = w.add_object(sh.as_pdf_object())
    new_page_tree = w.import_object(
        r.root.raw_get('/Pages'),
    )
    w.root['/Pages'] = new_page_tree
    out = BytesIO()
    w.write(out)
    return out


@pytest.mark.parametrize("use_owner_pass,rev,keylen_bytes,use_aes", [
    (True, StandardSecuritySettingsRevision.RC4_BASIC, 5, False),
    (False, StandardSecuritySettingsRevision.RC4_BASIC, 5, False),
    (True, StandardSecuritySettingsRevision.RC4_EXTENDED, 5, False),
    (False, StandardSecuritySettingsRevision.RC4_EXTENDED, 5, False),
    (True, StandardSecuritySettingsRevision.RC4_EXTENDED, 16, False),
    (False, StandardSecuritySettingsRevision.RC4_EXTENDED, 16, False),
    (True, StandardSecuritySettingsRevision.RC4_OR_AES128, 5, False),
    (False, StandardSecuritySettingsRevision.RC4_OR_AES128, 5, False),
    (True, StandardSecuritySettingsRevision.RC4_OR_AES128, 16, False),
    (False, StandardSecuritySettingsRevision.RC4_OR_AES128, 16, False),
    (True, StandardSecuritySettingsRevision.RC4_OR_AES128, 16, True),
    (False, StandardSecuritySettingsRevision.RC4_OR_AES128, 16, True),
])
def test_legacy_encryption(use_owner_pass, rev, keylen_bytes, use_aes):
    out = _produce_legacy_encrypted_file(rev, keylen_bytes, use_aes)
    r = PdfFileReader(out)
    result = r.decrypt("ownersecret" if use_owner_pass else "usersecret")
    if use_owner_pass:
        assert result.status == AuthStatus.OWNER
        assert result.permission_flags is None
    else:
        assert result.status == AuthStatus.USER
        assert result.permission_flags == -44
    page = r.root['/Pages']['/Kids'][0].get_object()
    assert r.trailer['/Encrypt']['/P'] == -44
    assert '/ExtGState' in page['/Resources']
    # just a piece of data I know occurs in the decoded content stream
    # of the (only) page in VECTOR_IMAGE_PDF
    assert b'0 1 0 rg /a0 gs' in page['/Contents'].data


@pytest.mark.parametrize("legacy", [True, False])
def test_wrong_password(legacy):
    w = writer.PdfFileWriter()
    ref = w.add_object(generic.TextStringObject("Blah blah"))
    if legacy:
        sh = StandardSecurityHandler.build_from_pw_legacy(
            StandardSecuritySettingsRevision.RC4_OR_AES128,
            w._document_id[0].original_bytes, "ownersecret", "usersecret",
            keylen_bytes=16, use_aes128=True
        )
    else:
        sh = StandardSecurityHandler.build_from_pw("ownersecret", "usersecret")
    w.security_handler = sh
    w._encrypt = w.add_object(sh.as_pdf_object())
    out = BytesIO()
    w.write(out)
    r = PdfFileReader(out)
    with pytest.raises(misc.PdfReadError):
        r.get_object(ref.reference)
    assert r.decrypt("thispasswordiswrong").status == AuthStatus.FAILED
    assert r.security_handler._auth_failed
    assert r.security_handler.get_string_filter()._auth_failed
    with pytest.raises(misc.PdfReadError):
        r.get_object(ref.reference)


def test_identity_crypt_filter_api():

    # confirm that the CryptFilter API of the identity filter doesn't do
    # anything unexpected, even though we typically don't invoke it explicitly.
    idf: IdentityCryptFilter = IdentityCryptFilter()
    idf._set_security_handler(None)
    assert not idf._auth_failed
    assert isinstance(idf.derive_shared_encryption_key(), bytes)
    assert isinstance(idf.derive_object_key(1, 2), bytes)
    assert isinstance(idf.method, generic.NameObject)
    assert isinstance(idf.keylen, int)
    assert idf.decrypt(None, b'abc') == b'abc'
    assert idf.encrypt(None, b'abc') == b'abc'

    # can't serialise /Identity
    with pytest.raises(misc.PdfError):
        idf.as_pdf_object()


@pytest.mark.parametrize("use_alias, with_never_decrypt", [
    (True, False), (False, True), (False, False)
])
def test_identity_crypt_filter(use_alias, with_never_decrypt):
    w = writer.PdfFileWriter()
    sh = StandardSecurityHandler.build_from_pw("secret")
    w.security_handler = sh
    idf: IdentityCryptFilter = IdentityCryptFilter()
    assert sh.crypt_filter_config[pdf_name("/Identity")] is idf
    if use_alias:
        sh.crypt_filter_config._crypt_filters[pdf_name("/IdentityAlias")] = idf
        assert sh.crypt_filter_config[pdf_name("/IdentityAlias")] is idf
    if use_alias:
        # identity filter can't be serialised, so this should throw an error
        with pytest.raises(misc.PdfError):
            w._assign_security_handler(sh)
        return
    else:
        w._assign_security_handler(sh)
    test_bytes = b'This is some test data that should remain unencrypted.'
    test_stream = generic.StreamObject(
        stream_data=test_bytes, handler=sh
    )
    test_stream.apply_filter(
        "/Crypt", params={pdf_name("/Name"): pdf_name("/Identity")}
    )
    ref = w.add_object(test_stream).reference
    out = BytesIO()
    w.write(out)

    r = PdfFileReader(out)
    r.decrypt("secret")
    the_stream = r.get_object(ref, never_decrypt=with_never_decrypt)
    assert the_stream.encoded_data == test_bytes
    assert the_stream.data == test_bytes


def _produce_pubkey_encrypted_file(version, keylen, use_aes, use_crypt_filters):
    r = PdfFileReader(BytesIO(VECTOR_IMAGE_PDF))
    w = writer.PdfFileWriter()

    sh = PubKeySecurityHandler.build_from_certs(
        [PUBKEY_TEST_DECRYPTER.cert], keylen_bytes=keylen,
        version=version, use_aes=use_aes, use_crypt_filters=use_crypt_filters,
        perms=-44
    )
    w.security_handler = sh
    w._encrypt = w.add_object(sh.as_pdf_object())
    new_page_tree = w.import_object(r.root.raw_get('/Pages'),)
    w.root['/Pages'] = new_page_tree
    out = BytesIO()
    w.write(out)
    return out


@pytest.mark.parametrize("version, keylen, use_aes, use_crypt_filters", [
    (SecurityHandlerVersion.AES256, 32, True, True),
    (SecurityHandlerVersion.RC4_OR_AES128, 16, True, True),
    (SecurityHandlerVersion.RC4_OR_AES128, 16, False, True),
    (SecurityHandlerVersion.RC4_OR_AES128, 5, False, True),
    (SecurityHandlerVersion.RC4_40, 5, False, True),
    (SecurityHandlerVersion.RC4_40, 5, False, False),
    (SecurityHandlerVersion.RC4_LONGER_KEYS, 5, False, True),
    (SecurityHandlerVersion.RC4_LONGER_KEYS, 5, False, False),
    (SecurityHandlerVersion.RC4_LONGER_KEYS, 16, False, True),
    (SecurityHandlerVersion.RC4_LONGER_KEYS, 16, False, False),
])
def test_pubkey_encryption(version, keylen, use_aes, use_crypt_filters):
    out = _produce_pubkey_encrypted_file(
        version, keylen, use_aes, use_crypt_filters
    )
    r = PdfFileReader(out)
    result = r.decrypt_pubkey(PUBKEY_TEST_DECRYPTER)
    assert result.status == AuthStatus.USER
    assert result.permission_flags == -44
    page = r.root['/Pages']['/Kids'][0].get_object()
    assert '/ExtGState' in page['/Resources']
    # just a piece of data I know occurs in the decoded content stream
    # of the (only) page in VECTOR_IMAGE_PDF
    assert b'0 1 0 rg /a0 gs' in page['/Contents'].data


def test_key_encipherment_requirement():
    with pytest.raises(misc.PdfWriteError):
        PubKeySecurityHandler.build_from_certs(
            [PUBKEY_SELFSIGNED_DECRYPTER.cert], keylen_bytes=32,
            version=SecurityHandlerVersion.AES256,
            use_aes=True, use_crypt_filters=True,
            perms=-44
        )


@pytest.mark.parametrize("version, keylen, use_aes, use_crypt_filters", [
    (SecurityHandlerVersion.AES256, 32, True, True),
    (SecurityHandlerVersion.RC4_OR_AES128, 16, True, True),
    (SecurityHandlerVersion.RC4_OR_AES128, 16, False, True),
    (SecurityHandlerVersion.RC4_OR_AES128, 5, False, True),
    (SecurityHandlerVersion.RC4_40, 5, False, True),
    (SecurityHandlerVersion.RC4_40, 5, False, False),
    (SecurityHandlerVersion.RC4_LONGER_KEYS, 5, False, True),
    (SecurityHandlerVersion.RC4_LONGER_KEYS, 5, False, False),
    (SecurityHandlerVersion.RC4_LONGER_KEYS, 16, False, True),
    (SecurityHandlerVersion.RC4_LONGER_KEYS, 16, False, False),
])
def test_key_encipherment_requirement_override(version, keylen, use_aes,
                                               use_crypt_filters):
    r = PdfFileReader(BytesIO(VECTOR_IMAGE_PDF))
    w = writer.PdfFileWriter()

    sh = PubKeySecurityHandler.build_from_certs(
        [PUBKEY_SELFSIGNED_DECRYPTER.cert], keylen_bytes=keylen,
        version=version, use_aes=use_aes, use_crypt_filters=use_crypt_filters,
        perms=-44, ignore_key_usage=True
    )
    w.security_handler = sh
    w._encrypt = w.add_object(sh.as_pdf_object())
    new_page_tree = w.import_object(
        r.root.raw_get('/Pages'),
    )
    w.root['/Pages'] = new_page_tree
    out = BytesIO()
    w.write(out)
    r = PdfFileReader(out)
    result = r.decrypt_pubkey(PUBKEY_SELFSIGNED_DECRYPTER)
    assert result.status == AuthStatus.USER
    assert result.permission_flags == -44
    page = r.root['/Pages']['/Kids'][0].get_object()
    assert '/ExtGState' in page['/Resources']
    # just a piece of data I know occurs in the decoded content stream
    # of the (only) page in VECTOR_IMAGE_PDF
    assert b'0 1 0 rg /a0 gs' in page['/Contents'].data


def test_pubkey_alternative_filter():
    w = writer.PdfFileWriter()

    w.encrypt_pubkey([PUBKEY_TEST_DECRYPTER.cert])
    # subfilter should be picked up
    w._encrypt.get_object()['/Filter'] = pdf_name('/FooBar')
    out = BytesIO()
    w.write(out)
    r = PdfFileReader(out)
    assert isinstance(r.security_handler, PubKeySecurityHandler)


@pytest.mark.parametrize('delete_subfilter', [True, False])
def test_pubkey_unsupported_filter(delete_subfilter):
    w = writer.PdfFileWriter()

    w.encrypt_pubkey([PUBKEY_TEST_DECRYPTER.cert])
    encrypt = w._encrypt.get_object()
    encrypt['/Filter'] = pdf_name('/FooBar')
    if delete_subfilter:
        del encrypt['/SubFilter']
    else:
        encrypt['/SubFilter'] = pdf_name('/baz.quux')
    out = BytesIO()
    w.write(out)
    with pytest.raises(misc.PdfReadError):
        PdfFileReader(out)


def test_pubkey_encryption_block_cfs_s4():
    w = writer.PdfFileWriter()

    w.encrypt_pubkey([PUBKEY_TEST_DECRYPTER.cert])
    encrypt = w._encrypt.get_object()
    encrypt['/SubFilter'] = pdf_name('/adbe.pkcs7.s4')
    out = BytesIO()
    w.write(out)
    with pytest.raises(misc.PdfReadError):
        PdfFileReader(out)


def test_pubkey_encryption_s5_requires_cfs():
    w = writer.PdfFileWriter()

    sh = PubKeySecurityHandler.build_from_certs([PUBKEY_TEST_DECRYPTER.cert])
    w._assign_security_handler(sh)
    encrypt = w._encrypt.get_object()
    del encrypt['/CF']
    out = BytesIO()
    w.write(out)
    with pytest.raises(misc.PdfReadError):
        PdfFileReader(out)


def test_pubkey_encryption_dict_errors():
    sh = PubKeySecurityHandler.build_from_certs([PUBKEY_TEST_DECRYPTER.cert])

    encrypt = generic.DictionaryObject(sh.as_pdf_object())
    encrypt['/SubFilter'] = pdf_name('/asdflakdsjf')
    with pytest.raises(misc.PdfReadError):
        PubKeySecurityHandler.build(encrypt)

    encrypt = generic.DictionaryObject(sh.as_pdf_object())
    encrypt['/Length'] = generic.NumberObject(13)
    with pytest.raises(misc.PdfError):
        PubKeySecurityHandler.build(encrypt)

    encrypt = generic.DictionaryObject(sh.as_pdf_object())
    del encrypt['/CF']['/DefaultCryptFilter']['/CFM']
    with pytest.raises(misc.PdfReadError):
        PubKeySecurityHandler.build(encrypt)

    encrypt = generic.DictionaryObject(sh.as_pdf_object())
    del encrypt['/CF']['/DefaultCryptFilter']['/Recipients']
    with pytest.raises(misc.PdfReadError):
        PubKeySecurityHandler.build(encrypt)

    encrypt = generic.DictionaryObject(sh.as_pdf_object())
    encrypt['/CF']['/DefaultCryptFilter']['/CFM'] = pdf_name('/None')
    with pytest.raises(misc.PdfReadError):
        PubKeySecurityHandler.build(encrypt)


@pytest.mark.parametrize('with_hex_filter, main_unencrypted', [
    (True, False), (True, True), (False, True), (False, False)
])
def test_custom_crypt_filter(with_hex_filter, main_unencrypted):
    w = writer.PdfFileWriter()
    custom = pdf_name('/Custom')
    crypt_filters = {
        custom: StandardRC4CryptFilter(keylen=16),
    }
    if main_unencrypted:
        # streams/strings are unencrypted by default
        cfc = CryptFilterConfiguration(crypt_filters=crypt_filters)
        assert len(cfc.filters()) == 1
    else:
        crypt_filters[STD_CF] = StandardAESCryptFilter(keylen=16)
        cfc = CryptFilterConfiguration(
            crypt_filters=crypt_filters,
            default_string_filter=STD_CF, default_stream_filter=STD_CF
        )
        assert len(cfc.filters()) == 2
    sh = StandardSecurityHandler.build_from_pw_legacy(
        rev=StandardSecuritySettingsRevision.RC4_OR_AES128,
        id1=w.document_id[0], desired_user_pass="usersecret",
        desired_owner_pass="ownersecret",
        keylen_bytes=16, crypt_filter_config=cfc
    )
    w._assign_security_handler(sh)
    test_data = b'This is test data!'
    dummy_stream = generic.StreamObject(stream_data=test_data)
    dummy_stream.add_crypt_filter(name=custom, handler=sh)
    ref = w.add_object(dummy_stream)
    dummy_stream2 = generic.StreamObject(stream_data=test_data)
    ref2 = w.add_object(dummy_stream2)

    if with_hex_filter:
        dummy_stream.apply_filter(pdf_name('/AHx'))
    out = BytesIO()
    w.write(out)
    r = PdfFileReader(out)
    r.decrypt("ownersecret")
    obj: generic.StreamObject = r.get_object(ref.reference)
    assert obj.data == test_data
    if with_hex_filter:
        cf_dict = obj['/DecodeParms'][1]
    else:
        cf_dict = obj['/DecodeParms']

    assert cf_dict['/Name'] == pdf_name('/Custom')

    obj2: generic.DecryptedObjectProxy = r.get_object(
        ref2.reference, transparent_decrypt=False
    )
    raw = obj2.raw_object
    assert isinstance(raw, generic.StreamObject)
    if main_unencrypted:
        assert raw.encoded_data == test_data
    else:
        assert raw.encoded_data != test_data


@pytest.mark.parametrize('with_hex_filter, main_unencrypted', [
    (True, False), (True, True), (False, True), (False, False)
])
def test_custom_pubkey_crypt_filter(with_hex_filter, main_unencrypted):
    w = writer.PdfFileWriter()
    custom = pdf_name('/Custom')
    crypt_filters = {
        custom: PubKeyRC4CryptFilter(keylen=16),
    }
    if main_unencrypted:
        # streams/strings are unencrypted by default
        cfc = CryptFilterConfiguration(crypt_filters=crypt_filters)
    else:
        crypt_filters[DEFAULT_CRYPT_FILTER] = PubKeyAESCryptFilter(
            keylen=16, acts_as_default=True
        )
        cfc = CryptFilterConfiguration(
            crypt_filters=crypt_filters,
            default_string_filter=DEFAULT_CRYPT_FILTER,
            default_stream_filter=DEFAULT_CRYPT_FILTER
        )
    sh = PubKeySecurityHandler(
        version=SecurityHandlerVersion.RC4_OR_AES128,
        pubkey_handler_subfilter=PubKeyAdbeSubFilter.S5,
        legacy_keylen=16, crypt_filter_config=cfc
    )

    # if main_unencrypted, these should be no-ops
    sh.add_recipients([PUBKEY_TEST_DECRYPTER.cert])
    # (this is always pointless, but it should be allowed)
    sh.add_recipients([PUBKEY_TEST_DECRYPTER.cert])

    crypt_filters[custom].add_recipients([PUBKEY_TEST_DECRYPTER.cert])
    w._assign_security_handler(sh)

    encrypt_dict = w._encrypt.get_object()
    cfs = encrypt_dict['/CF']
    # no /Recipients in S5 mode
    assert '/Recipients' not in encrypt_dict
    assert isinstance(cfs[custom]['/Recipients'], generic.ByteStringObject)
    if main_unencrypted:
        assert DEFAULT_CRYPT_FILTER not in cfs
    else:
        default_rcpts = cfs[DEFAULT_CRYPT_FILTER]['/Recipients']
        assert isinstance(default_rcpts, generic.ArrayObject)
        assert len(default_rcpts) == 2

    # custom crypt filters can only have one set of recipients
    with pytest.raises(misc.PdfError):
        crypt_filters[custom].add_recipients([PUBKEY_TEST_DECRYPTER.cert])

    test_data = b'This is test data!'
    dummy_stream = generic.StreamObject(stream_data=test_data)
    dummy_stream.add_crypt_filter(name=custom, handler=sh)
    ref = w.add_object(dummy_stream)
    dummy_stream2 = generic.StreamObject(stream_data=test_data)
    ref2 = w.add_object(dummy_stream2)

    if with_hex_filter:
        dummy_stream.apply_filter(pdf_name('/AHx'))
    out = BytesIO()
    w.write(out)
    r = PdfFileReader(out)
    r.decrypt_pubkey(PUBKEY_TEST_DECRYPTER)

    # the custom test filter shouldn't have been decrypted yet
    # so attempting to decode the stream should cause the crypt filter
    # to throw an error
    obj: generic.StreamObject = r.get_object(ref.reference)
    with pytest.raises(misc.PdfError):
        # noinspection PyStatementEffect
        obj.data

    r.security_handler.crypt_filter_config[custom].authenticate(
        PUBKEY_TEST_DECRYPTER
    )
    assert obj.data == test_data
    if with_hex_filter:
        cf_dict = obj['/DecodeParms'][1]
    else:
        cf_dict = obj['/DecodeParms']

    assert cf_dict['/Name'] == pdf_name('/Custom')

    obj2: generic.DecryptedObjectProxy = r.get_object(
        ref2.reference, transparent_decrypt=False
    )
    raw = obj2.raw_object
    assert isinstance(raw, generic.StreamObject)
    if main_unencrypted:
        assert raw.encoded_data == test_data
    else:
        assert raw.encoded_data != test_data


def test_custom_crypt_filter_errors():
    w = writer.PdfFileWriter()
    custom = pdf_name('/Custom')
    crypt_filters = {
        custom: StandardRC4CryptFilter(keylen=16),
        STD_CF: StandardAESCryptFilter(keylen=16)
    }
    cfc = CryptFilterConfiguration(
        crypt_filters=crypt_filters,
        default_string_filter=STD_CF, default_stream_filter=STD_CF
    )
    sh = StandardSecurityHandler.build_from_pw_legacy(
        rev=StandardSecuritySettingsRevision.RC4_OR_AES128,
        id1=w.document_id[0], desired_user_pass="usersecret",
        desired_owner_pass="ownersecret",
        keylen_bytes=16, crypt_filter_config=cfc
    )
    w._assign_security_handler(sh)
    test_data = b'This is test data!'
    dummy_stream = generic.StreamObject(stream_data=test_data)
    with pytest.raises(misc.PdfStreamError):
        dummy_stream.add_crypt_filter(name='/Idontexist', handler=sh)

    # no handler
    dummy_stream.add_crypt_filter(name=custom)
    dummy_stream._handler = None
    w.add_object(dummy_stream)

    out = BytesIO()
    with pytest.raises(misc.PdfStreamError):
        w.write(out)


def test_continue_encrypted_file_without_auth():
    w = writer.PdfFileWriter()
    w.root["/Test"] = generic.TextStringObject("Blah blah")
    w.encrypt("ownersecret", "usersecret")
    out = BytesIO()
    w.write(out)
    incr_w = IncrementalPdfFileWriter(out)
    incr_w.root["/Test"] = generic.TextStringObject("Bluh bluh")
    incr_w.update_root()
    with pytest.raises(misc.PdfWriteError):
        incr_w.write_in_place()


def test_continue_encrypted_file_from_reader():
    w = writer.PdfFileWriter()
    w.root["/Test"] = generic.TextStringObject("Blah blah")
    w.encrypt("ownersecret", "usersecret")
    out = BytesIO()
    w.write(out)
    r = PdfFileReader(out)
    # first decrypt, then extend
    r.decrypt("usersecret")
    incr_w = IncrementalPdfFileWriter.from_reader(r)
    incr_w.root["/Test"] = generic.TextStringObject("Bluh bluh")
    incr_w.update_root()
    incr_w.write_in_place()

    r = PdfFileReader(out)
    r.decrypt("usersecret")
    assert r.root['/Test'] == generic.TextStringObject("Bluh bluh")


def test_aes256_perm_read():
    r = PdfFileReader(BytesIO(MINIMAL_ONE_FIELD_AES256))
    result = r.decrypt("ownersecret")
    assert result.permission_flags is None
    r = PdfFileReader(BytesIO(MINIMAL_ONE_FIELD_AES256))
    result = r.decrypt("usersecret")
    assert result.permission_flags == -4

    assert r.trailer['/Encrypt']['/P'] == -4


def test_copy_encrypted_file():
    r = PdfFileReader(BytesIO(MINIMAL_ONE_FIELD_AES256))
    r.decrypt("ownersecret")
    w = writer.copy_into_new_writer(r)
    old_root_ref = w.root_ref
    out = BytesIO()
    w.write(out)
    r = PdfFileReader(out)
    assert r.root_ref == old_root_ref
    assert len(r.root['/AcroForm']['/Fields']) == 1
    assert len(r.root['/Pages']['/Kids']) == 1


def test_copy_to_encrypted_file():
    r = PdfFileReader(BytesIO(MINIMAL_ONE_FIELD))
    w = writer.copy_into_new_writer(r)
    old_root_ref = w.root_ref
    w.encrypt("ownersecret", "usersecret")
    out = BytesIO()
    w.write(out)
    r = PdfFileReader(out)
    result = r.decrypt("ownersecret")
    assert result.status == AuthStatus.OWNER
    assert r.root_ref == old_root_ref
    assert len(r.root['/AcroForm']['/Fields']) == 1
    assert len(r.root['/Pages']['/Kids']) == 1


def test_empty_user_pass():
    r = PdfFileReader(BytesIO(MINIMAL_ONE_FIELD))
    w = writer.copy_into_new_writer(r)
    old_root_ref = w.root_ref
    w.encrypt('ownersecret', '')
    out = BytesIO()
    w.write(out)
    r = PdfFileReader(out)
    result = r.decrypt('')
    assert result.status == AuthStatus.USER
    assert r.root_ref == old_root_ref
    assert len(r.root['/AcroForm']['/Fields']) == 1
    assert len(r.root['/Pages']['/Kids']) == 1
    assert r.trailer['/Info']['/Producer'].startswith('pyHanko')


def test_load_pkcs12():

    sedk = SimpleEnvelopeKeyDecrypter.load_pkcs12(
        "pyhanko_tests/data/crypto/selfsigned.pfx", b'exportsecret'
    )
    assert sedk.cert.subject == PUBKEY_SELFSIGNED_DECRYPTER.cert.subject


def test_pubkey_wrong_cert():
    r = PdfFileReader(BytesIO(VECTOR_IMAGE_PDF))
    w = writer.PdfFileWriter()

    recpt_cert = load_cert_from_pemder(
        TESTING_CA_DIR + '/interm/decrypter2.cert.pem'
    )
    test_data = b'This is test data!'
    dummy_stream = generic.StreamObject(stream_data=test_data)
    ref = w.add_object(dummy_stream)
    w.encrypt_pubkey([recpt_cert])
    out = BytesIO()
    w.write(out)
    r = PdfFileReader(out)
    result = r.decrypt_pubkey(PUBKEY_TEST_DECRYPTER)
    assert result.status == AuthStatus.FAILED

    with pytest.raises(misc.PdfError):
        r.get_object(ref.reference)


def test_crypt_filter_build_failures():
    cfdict = generic.DictionaryObject()
    assert build_crypt_filter({}, cfdict, False) is None
    cfdict['/CFM'] = generic.NameObject('/None')
    assert build_crypt_filter({}, cfdict, False) is None

    with pytest.raises(NotImplementedError):
        cfdict['/CFM'] = generic.NameObject('/NoSuchCF')
        build_crypt_filter({}, cfdict, False)


@pytest.mark.parametrize('on_subclass', [True, False])
def test_custom_crypt_filter_type(on_subclass):
    w = writer.PdfFileWriter()
    custom_cf_type = pdf_name('/CustomCFType')

    class CustomCFClass(StandardRC4CryptFilter):
        def __init__(self):
            super().__init__(keylen=16)
        method = custom_cf_type

    if on_subclass:
        class NewStandardSecurityHandler(StandardSecurityHandler):
            pass
        sh_class = NewStandardSecurityHandler
        assert sh_class._known_crypt_filters is \
               not StandardSecurityHandler._known_crypt_filters
        assert '/V2' in sh_class._known_crypt_filters
        SecurityHandler.register(sh_class)
    else:
        sh_class = StandardSecurityHandler

    sh_class.register_crypt_filter(
        custom_cf_type, lambda _, __: CustomCFClass(),
    )
    cfc = CryptFilterConfiguration(
        crypt_filters={STD_CF: CustomCFClass()},
        default_string_filter=STD_CF, default_stream_filter=STD_CF
    )
    sh = sh_class.build_from_pw_legacy(
        rev=StandardSecuritySettingsRevision.RC4_OR_AES128,
        id1=w.document_id[0], desired_user_pass="usersecret",
        desired_owner_pass="ownersecret",
        keylen_bytes=16, crypt_filter_config=cfc
    )
    assert isinstance(sh, sh_class)
    w._assign_security_handler(sh)
    test_data = b'This is test data!'
    dummy_stream = generic.StreamObject(stream_data=test_data)
    ref = w.add_object(dummy_stream)

    out = BytesIO()
    w.write(out)
    r = PdfFileReader(out)
    r.decrypt("ownersecret")

    cfc = r.security_handler.crypt_filter_config
    assert cfc.stream_filter_name == cfc.string_filter_name
    obj: generic.StreamObject = r.get_object(ref.reference)
    assert obj.data == test_data

    obj: generic.DecryptedObjectProxy = \
        r.get_object(ref.reference, transparent_decrypt=False)
    assert isinstance(obj.raw_object, generic.StreamObject)
    assert obj.raw_object.encoded_data != test_data

    # restore security handler registry state
    del sh_class._known_crypt_filters[custom_cf_type]
    if on_subclass:
        SecurityHandler.register(StandardSecurityHandler)


def test_security_handler_version_deser():
    assert SecurityHandlerVersion.from_number(5) \
           == SecurityHandlerVersion.AES256
    assert SecurityHandlerVersion.from_number(6) == SecurityHandlerVersion.OTHER
    assert SecurityHandlerVersion.from_number(None) \
           == SecurityHandlerVersion.OTHER

    assert StandardSecuritySettingsRevision.from_number(6) \
           == StandardSecuritySettingsRevision.AES256
    assert StandardSecuritySettingsRevision.from_number(7) \
           == StandardSecuritySettingsRevision.OTHER


def test_key_len():
    with pytest.raises(misc.PdfError):
        SecurityHandlerVersion.RC4_OR_AES128.check_key_length(20)
    assert SecurityHandlerVersion.RC4_OR_AES128.check_key_length(6) == 6
    assert SecurityHandlerVersion.AES256.check_key_length(6) == 32
    assert SecurityHandlerVersion.RC4_40.check_key_length(32) == 5
    assert SecurityHandlerVersion.RC4_LONGER_KEYS.check_key_length(16) == 16


@pytest.mark.parametrize('pw', ['usersecret', 'ownersecret'])
def test_ser_deser_credential_standard_sh(pw):
    r = PdfFileReader(BytesIO(MINIMAL_AES256))
    r.decrypt(pw)
    cred = r.security_handler.extract_credential()
    assert cred['pwd_bytes'].native == pw.encode('utf8')
    cred_data = cred.serialise()

    r = PdfFileReader(BytesIO(MINIMAL_AES256))
    result = r.security_handler.authenticate(cred_data)
    exp_status = AuthStatus.USER if pw.startswith('user') else AuthStatus.OWNER
    assert result.status == exp_status


def test_ser_deser_credential_standard_sh_extract_from_builder():
    sh = StandardSecurityHandler.build_from_pw("ownersecret", "usersecret")
    cred = sh.extract_credential()
    assert cred['pwd_bytes'].native == b'ownersecret'
    assert cred['id1'].native is None


def test_ser_deser_credential_wrong_pw():
    r = PdfFileReader(BytesIO(MINIMAL_AES256))
    r.decrypt("ownersecret")
    cred = r.security_handler.extract_credential()
    cred['pwd_bytes'] = b'This is the wrong password'
    cred_data = cred.serialise()

    r = PdfFileReader(BytesIO(MINIMAL_AES256))
    result = r.security_handler.authenticate(cred_data)
    assert result.status == AuthStatus.FAILED


def test_ser_deser_credential_standard_corrupted():
    r = PdfFileReader(BytesIO(MINIMAL_AES256))
    r.decrypt("ownersecret")
    cred = r.security_handler.extract_credential()
    cred_data = SerialisedCredential(
        credential_type=cred.serialise().credential_type,
        data=b'\xde\xad\xbe\xef'
    )

    r = PdfFileReader(BytesIO(MINIMAL_AES256))
    with pytest.raises(misc.PdfReadError,
                       match="Failed to deserialise password"):
        r.security_handler.authenticate(cred_data)


def test_ser_deser_credential_unknown_cred_type():
    r = PdfFileReader(BytesIO(MINIMAL_AES256))
    r.decrypt("ownersecret")
    cred = r.security_handler.extract_credential()
    cred_data = SerialisedCredential(
        credential_type='foobar',
        data=cred.serialise().data
    )

    r = PdfFileReader(BytesIO(MINIMAL_AES256))
    with pytest.raises(misc.PdfReadError,
                       match="credential type 'foobar' not known"):
        r.security_handler.authenticate(cred_data)


@pytest.mark.parametrize('pw', ['usersecret', 'ownersecret'])
def test_ser_deser_credential_standard_sh_legacy(pw):
    out = _produce_legacy_encrypted_file(
        StandardSecuritySettingsRevision.RC4_OR_AES128, 16, True
    )
    r = PdfFileReader(out)
    r.decrypt(pw)
    cred = r.security_handler.extract_credential()
    assert cred['pwd_bytes'].native == pw.encode('utf8')
    assert cred['id1'].native is not None
    cred_data = cred.serialise()

    r = PdfFileReader(BytesIO(MINIMAL_AES256))
    result = r.security_handler.authenticate(cred_data)
    exp_status = AuthStatus.USER if pw.startswith('user') else AuthStatus.OWNER
    assert result.status == exp_status


@pytest.mark.parametrize('pw', ['usersecret', 'ownersecret'])
def test_ser_deser_credential_standard_sh_legacy_no_id1(pw):
    out = _produce_legacy_encrypted_file(
        StandardSecuritySettingsRevision.RC4_OR_AES128, 16, True
    )
    r = PdfFileReader(out)
    r.decrypt(pw)
    cred = r.security_handler.extract_credential()
    del cred['id1']
    cred_data = cred.serialise()

    r = PdfFileReader(out)
    with pytest.raises(misc.PdfReadError, match="id1"):
        r.security_handler.authenticate(cred_data)


def test_ser_deser_credential_standard_legacy_sh_extract_from_builder():
    sh = StandardSecurityHandler.build_from_pw_legacy(
        desired_owner_pass=b'ownersecret', desired_user_pass=b'usersecret',
        rev=StandardSecuritySettingsRevision.RC4_OR_AES128, keylen_bytes=16,
        id1=b'\xde\xad\xbe\xef'
    )
    cred = sh.extract_credential()
    assert cred['pwd_bytes'].native == b'ownersecret'
    assert cred['id1'].native == b'\xde\xad\xbe\xef'


def test_ser_deser_credential_pubkey():
    out = _produce_pubkey_encrypted_file(
        SecurityHandlerVersion.RC4_OR_AES128, 16, True, True
    )
    r = PdfFileReader(out)
    r.decrypt_pubkey(PUBKEY_TEST_DECRYPTER)
    cred_data = r.security_handler.extract_credential().serialise()

    r = PdfFileReader(out)
    result = r.security_handler.authenticate(cred_data)
    assert result.status == AuthStatus.USER


def test_ser_deser_credential_pubkey_sh_cannot_extract_from_builder():
    sh = PubKeySecurityHandler.build_from_certs(
        [PUBKEY_TEST_DECRYPTER.cert], keylen_bytes=16,
        version=SecurityHandlerVersion.RC4_OR_AES128,
        use_aes=True, use_crypt_filters=True,
        perms=-44
    )
    assert sh.extract_credential() is None


def test_ser_deser_credential_wrong_cred_type_pubkey():
    r = PdfFileReader(BytesIO(MINIMAL_AES256))
    r.decrypt("ownersecret")
    cred_data = r.security_handler.extract_credential().serialise()

    out = _produce_pubkey_encrypted_file(
        SecurityHandlerVersion.RC4_OR_AES128, 16, True, True
    )
    r = PdfFileReader(out)
    with pytest.raises(misc.PdfReadError,
                       match="must be an instance of"):
        r.security_handler.authenticate(cred_data)


def test_ser_deser_credential_wrong_cred_type_standard():
    out = _produce_pubkey_encrypted_file(
        SecurityHandlerVersion.RC4_OR_AES128, 16, True, True
    )
    r = PdfFileReader(out)
    r.decrypt_pubkey(PUBKEY_TEST_DECRYPTER)
    cred_data = r.security_handler.extract_credential().serialise()

    r = PdfFileReader(BytesIO(MINIMAL_AES256))
    with pytest.raises(misc.PdfReadError, match="Standard auth.*must be a"):
        r.security_handler.authenticate(cred_data)


def test_ser_deser_credential_pubkey_corrupted():
    out = _produce_pubkey_encrypted_file(
        SecurityHandlerVersion.RC4_OR_AES128, 16, True, True
    )
    r = PdfFileReader(out)
    r.decrypt_pubkey(PUBKEY_TEST_DECRYPTER)
    cred = r.security_handler.extract_credential()
    cred_data = SerialisedCredential(
        credential_type=cred.serialise().credential_type,
        data=b'\xde\xad\xbe\xef'
    )

    r = PdfFileReader(out)
    with pytest.raises(misc.PdfReadError,
                       match="Failed to decode serialised pubkey credential"):
        r.security_handler.authenticate(cred_data)


def test_ser_deser_credential_wrong_cert():

    wrong_cert_cred_data = SimpleEnvelopeKeyDecrypter(
        cert=PUBKEY_SELFSIGNED_DECRYPTER.cert,
        private_key=PUBKEY_TEST_DECRYPTER.private_key
    ).serialise()
    out = _produce_pubkey_encrypted_file(
        SecurityHandlerVersion.RC4_OR_AES128, 16, True, True
    )
    r = PdfFileReader(out)

    result = r.security_handler.authenticate(wrong_cert_cred_data)
    assert result.status == AuthStatus.FAILED


def test_ser_deser_credential_wrong_key():

    wrong_key_cred_data = SimpleEnvelopeKeyDecrypter(
        cert=PUBKEY_TEST_DECRYPTER.cert,
        private_key=PUBKEY_SELFSIGNED_DECRYPTER.private_key
    ).serialise()
    out = _produce_pubkey_encrypted_file(
        SecurityHandlerVersion.RC4_OR_AES128, 16, True, True
    )
    r = PdfFileReader(out)

    # we're OK with this being an error, since a certificate match with a wrong
    # key is almost certainly indicative of something that shouldn't happen
    # in regular usage.
    with pytest.raises(misc.PdfReadError, match="envelope key"):
        r.security_handler.authenticate(wrong_key_cred_data)


@pytest.mark.parametrize('legacy', [True, False])
def test_encrypt_skipping_metadata(legacy):
    # we need to manually flag the metadata streams, since
    # pyHanko's PDF reader is (currently) not metadata-aware
    from pyhanko.pdf_utils.writer import copy_into_new_writer
    with open(os.path.join(PDF_DATA_DIR, "minimal-pdf-ua-and-a.pdf"), 'rb') \
            as inf:
        w = copy_into_new_writer(PdfFileReader(inf))

    if legacy:
        sh = StandardSecurityHandler.build_from_pw_legacy(
            StandardSecuritySettingsRevision.RC4_OR_AES128,
            w._document_id[0].original_bytes,
            desired_owner_pass="secret", desired_user_pass="secret",
            keylen_bytes=16, use_aes128=True,
            perms=-44,
            encrypt_metadata=False
        )
        w._assign_security_handler(sh)
    else:
        w.encrypt("secret", "secret", encrypt_metadata=False)
    w.root['/Metadata'].apply_filter(
        "/Crypt", params={pdf_name("/Name"): pdf_name("/Identity")}
    )

    out = BytesIO()
    w.write(out)

    out.seek(0)
    r = PdfFileReader(out)
    mtd = r.root['/Metadata']
    assert not r.trailer['/Encrypt']['/EncryptMetadata']
    assert b'Test document' in mtd.encoded_data
    assert b'Test document' in mtd.data
    result = r.decrypt("secret")
    assert result.status == AuthStatus.OWNER

    assert r.trailer['/Info']['/Title'] == 'Test document'


def test_encrypt_skipping_metadata_pubkey():
    # we need to manually flag the metadata streams, since
    # pyHanko's PDF reader is (currently) not metadata-aware
    from pyhanko.pdf_utils.writer import copy_into_new_writer
    with open(os.path.join(PDF_DATA_DIR, "minimal-pdf-ua-and-a.pdf"), 'rb') \
            as inf:
        w = copy_into_new_writer(PdfFileReader(inf))

    w.encrypt_pubkey([PUBKEY_TEST_DECRYPTER.cert], encrypt_metadata=False)
    w.root['/Metadata'].apply_filter(
        "/Crypt", params={pdf_name("/Name"): pdf_name("/Identity")}
    )

    out = BytesIO()
    w.write(out)

    out.seek(0)
    r = PdfFileReader(out)
    mtd = r.root['/Metadata']
    assert b'Test document' in mtd.encoded_data
    assert b'Test document' in mtd.data
    result = r.decrypt_pubkey(PUBKEY_TEST_DECRYPTER)
    assert result.status == AuthStatus.USER

    assert r.trailer['/Info']['/Title'] == 'Test document'


def test_pubkey_rc4_envelope():
    fname = os.path.join(PDF_DATA_DIR, "minimal-pubkey-rc4-envelope.pdf")
    with open(fname, 'rb') as inf:
        r = PdfFileReader(inf)
        result = r.decrypt_pubkey(PUBKEY_TEST_DECRYPTER)
        assert result.status == AuthStatus.USER
        assert b'Hello' in r.root['/Pages']['/Kids'][0]['/Contents'].data


def test_unknown_envelope_enc_type():
    fname = os.path.join(
        PDF_DATA_DIR, "minimal-pubkey-unknown-envelope-alg.pdf"
    )
    with open(fname, 'rb') as inf:
        r = PdfFileReader(inf)
        with pytest.raises(misc.PdfError, match="Cipher.*not allowed"):
            r.decrypt_pubkey(PUBKEY_TEST_DECRYPTER)
