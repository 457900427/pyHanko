import click
import getpass
from . import sign
from pdf_utils.incremental_writer import IncrementalPdfFileWriter

__all__ = ['cli']

# group everything under this entry point for easy exporting
@click.group()
def cli():
    pass


@cli.group(help='sign PDF files', name='sign')
def signing():
    pass

SIG_META = 'SIG_META'
EXISTING_ONLY = 'EXISTING_ONLY'

readable_file = click.Path(exists=True, readable=True, dir_okay=False)

@signing.group(name='addsig', help='add a signature')
# TODO this shouldn't always be required, really
@click.option('--field', help='name of the signature field', required=True)
@click.option('--name', help='explicitly specify signer name', required=False)
@click.option('--reason', help='reason for signing', required=False)
@click.option('--location', help='location of signing', required=False)
@click.option('--certify', help='add certification signature', required=False, 
              default=False, is_flag=True, type=bool, show_default=True)
@click.option('--existing-only', help='never create signature fields', 
              required=False, default=False, is_flag=True, type=bool, 
              show_default=True)
@click.pass_context
def addsig(ctx, field, name, reason, location, certify, existing_only):
    ctx.ensure_object(dict)
    ctx.obj[EXISTING_ONLY] = existing_only
    ctx.obj[SIG_META] = sign.PdfSignatureMetadata(
        field_name=field, location=location, reason=reason, name=name,
        certify=certify
    )

# TODO PKCS12 support
@addsig.command(name='pemder', help='read key material from PEM/DER files')
@click.argument('infile', type=click.File('rb'))
@click.argument('outfile', type=click.File('wb'))
@click.option('--key', help='file containing the private key (PEM/DER)', 
              type=readable_file, required=True)
@click.option('--cert', help='file containing the signer\'s certificate '
              '(PEM/DER)', type=readable_file, required=True)
# TODO allow reading the passphrase from a specific file descriptor
#  (for advanced scripting setups)
@click.option('--passfile', help='file containing the passphrase '
              'for the private key', required=False, type=click.File('rb'),
              show_default='stdin')
@click.pass_context
def addsig_pemder(ctx, infile, outfile, key, cert, passfile):
    signature_meta = ctx.obj[SIG_META]
    existing_fields_only = ctx.obj[EXISTING_ONLY]

    if passfile is None:
        passphrase = getpass.getpass(prompt='Key passphrase: ').encode('utf-8')
    else:
        passphrase = passfile.read()
        passfile.close()
    
    signer = sign.SimpleSigner.load(
        cert_file=cert, key_file=key, key_passphrase=passphrase
    )

    result = sign.sign_pdf(
        IncrementalPdfFileWriter(infile), signature_meta, signer,
        existing_fields_only=existing_fields_only
    )
    buf = result.getbuffer()
    outfile.write(buf)
    buf.release()

    infile.close()
    outfile.close()


@addsig.command(name='beid', help='use Belgian eID to sign')
@click.argument('infile', type=click.File('rb'))
@click.argument('outfile', type=click.File('wb'))
@click.option('--lib', help='path to libbeidpkcs11.so', 
              type=readable_file, required=True)
@click.option('--use-signature-cert', type=bool, show_default=True,
              default=True, required=False, is_flag=True,
              help='when false, use Authentication cert')
@click.option('--slot-no', help='specify PKCS#11 slot to use', 
              required=False, type=int, default=None)
@click.pass_context
def addsig_beid(ctx, infile, outfile, lib, use_signature_cert, slot_no):
    from . import beid

    signature_meta = ctx.obj[SIG_META]
    existing_fields_only = ctx.obj[EXISTING_ONLY]
    session = beid.open_beid_session(lib, slot_no=slot_no)
    label = 'Signature' if use_signature_cert else 'Authentication'
    signer = beid.BEIDSigner(session, label)
    
    result = sign.sign_pdf(
        IncrementalPdfFileWriter(infile), signature_meta, signer,
        existing_fields_only=existing_fields_only, bytes_reserved=16384
    )
    buf = result.getbuffer()
    outfile.write(buf)
    buf.release()

    infile.close()
    outfile.close()